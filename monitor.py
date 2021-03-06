#!/usr/bin/env python
# encoding: utf-8
'''
monitor -- Monitors DTN flow data and stores the results in a sqlite database.

monitor is designed for Ubuntu and CentOS Linux running Python 2.7
Disclaimer: This is just "proof-of-concept" code. There is a much better way to
do many of these functions.

@author:     Nathan Hanford

@contact:    nhanford@es.net
@deffield    updated: Updated
'''

import sys,os,re,subprocess,socket,sched,time,datetime,threading,sqlite3,struct
import argparse,json,logging,warnings

__all__ = []
__version__ = 0.8
__date__ = '2015-06-22'
__updated__ = '2015-09-14'

SPEEDCLASSES = [(800,'1:2',1000),(4500,'1:3',5000),(9500,'1:4',10000)]

DEBUG = 0
TESTRUN = 1
SKIPAFFINITY = 1

#Error Handling: These are sort of placeholders for the impending
#modularization of this monolithic script. This will be done to allow the
#monitoring components to limp along, even if affinitization and throttling
#aren't working.

class CLIError(Exception):
    '''generic exception to raise and log different fatal errors'''
    def __init__(self, msg):
        super(CLIError).__init__(type(self))
        self.msg = 'E: {}'.format(msg)
    def __str__(self):
        return self.msg
    def __unicode__(self):
        return self.msg

class ProcError(Exception):
    '''
    generic exception to raise and log errors from accessing procfs
    These errors are fatal to the affinity tuning components and some monitoring components.
    '''
    def __init__(self, msg):
        self.msg = 'E: {}'.format(msg)
    def __str__(self):
        return self.msg
    def __unicode__(self):
        return self.msg

class DBError(Exception):
    '''
    generic exception to handle errors from the database
    These errors may be fatal to the ability to record flow data.
    '''
    def __init__(self, msg):
        self.msg = 'E: {}'.format(msg)
    def __str__(self):
        return self.msg
    def __unicode__(self):
        return self.msg

class SSError(Exception):
    '''
    generic exception to handle errors from ss
    These errors are fatal to the monitoring components.
    '''
    def __init__(self, msg):
        self.msg = 'E: {}'.format(msg)
    def __str__(self):
        return self.msg
    def __unicode__(self):
        return self.msg

class TCError(Exception):
    '''
    generic exception to handle errors from tc
    These errors are fatal to the throttling components.
    '''
    def __init__(self, msg):
        self.msg = 'E: {}'.format(msg)
    def __str__(self):
        return self.msg
    def __unicode__(self):
        return self.msg

def checkibalance():
    '''attempts to disable irqbalance'''
    #If irqbalance is not installed, this will "fail," but that's fine, as long as irqbalance isn't running
    stat = subprocess.check_call(['service','irqbalance','stop'])
    return 0

def pollcpu():
    '''determines the number of cpus in the system'''
    pfile = open('/proc/cpuinfo','r')
    numcpus=0
    for line in pfile:
        line.strip()
        if re.search('processor',line):
            numcpus +=1
    pfile.close()
    return numcpus

def pollaffinity(irqlist):
    '''determines the current affinity scenario'''
    affinity = dict()
    for i in irqlist:
        #each of these files corresponds to the hexadecimal bitmask over which cpu(s) the irq is affinitized to.
        pfile = open('/proc/irq/{}/smp_affinity'.format(i),'r')
        thisAffinity=pfile.read().strip()
        affinity[i]=thisAffinity
        pfile.close()
    return affinity

def pollirq(iface):
    '''determines the irq numbers of the given interface'''
    irqfile = open('/proc/interrupts','r')
    irqlist=[]
    for line in irqfile:
        line.strip()
        line = re.search('.+'+iface,line)
        if(line):
            line = re.search('\d+:',line.group(0))
            line = re.search('\d+',line.group(0))
            irqlist.append(line.group(0))
    if any(irqlist):
        irqfile.close()
        return irqlist
    driver = subprocess.check_output(['ethtool','-i',iface])
    #Mellanox doesn't always report their NICs as ethX
    if 'mlx4' in driver:
        irqfile.seek(0)
        for line in irqfile:
            line.strip()
            line = re.search('.+'+'mlx4',line)
            if(line):
                line = re.search('\d+:',line.group(0))
                line = re.search('\d+',line.group(0))
                irqlist.append(line.group(0))
        irqfile.close()
        return irqlist
    raise ProcError(Exception,'unable to process /proc/interrupts')

def setaffinity(affy,numcpus):
    '''naively sets the affinity based on industry best practices for a multiqueue NIC'''
    #In order for RFS to do its job, each queue (represented by an irq#) needs to correspond to 1 and only 1 CPU.
    #The goals are as follows: use as many CPUs as possible, such that iff the number of irqs>number of CPUs, two irqs may share a CPU.
    #The target of further development will be to create a CPU blacklist of oversubscribed CPUs that we will exclude from this process based on a variety of factors.

    numdigits = numcpus/4
    mask = 1
    irqcount = 0
    for key in affy:
        if irqcount > numcpus - 1:
            mask = 1
            irqcount = 0
        strmask = '%x'.format(mask)
        while len(strmask)<numdigits:
            strmask = '0'+strmask
        mask = mask << 1
        smp = open('/proc/irq/{}/smp_affinity'.format(key),'w')
        smp.write(strmask)
        smp.close()
        irqcount +=1
    return

def setperformance(numcpus):
    '''sets all cpus to performance mode'''
    #This is perhaps somewhat contraversial in practice, but essential for accurate testing.
    #In practice, you are going to be disk-limited, but we're trying to eliminate the possibility of a CPU frequency scaling being the limiting factor.
    for i in range(numcpus):
        throttle = open('/sys/devices/system/cpu/cpu{}/cpufreq/scaling_governor'.format(i), 'w')
        throttle.write('performance')
        throttle.close()
    return

def getlinerate(iface):
    '''uses ethtool to determine the linerate of the selected interface'''
    #I think this is essential, and will certainly become essential for the receiver: You shouldn't throttle higher than your line rate (you might as well not throttle).
    out = subprocess.check_output(['ethtool',iface])
    speed = re.search('.+Speed:.+',out)
    speed = re.sub('.+Speed:\s','',speed.group(0))
    speed = re.sub('Mb/s','',speed)
    if 'Unknown' in speed:
        #There should be a better way to check that the interface is disabled...
        raise TCError(Exception,'Line rate for this interface is unknown: interface could be disabled')
    return speed

def setthrottles(iface):
    '''sets predefined common throttles in tc'''
    try:
        #Make sure there is no root qdisc
        stat = subprocess.check_call(['tc','qdisc','del','dev',iface,'root'])
    except subprocess.CalledProcessError as e:
        #Might get here because there was no root qdisc...
        if e.returncode != 2:
            raise e
    #Now we add an htb root qdisc
    subprocess.check_call(['tc','qdisc','add','dev',iface,'handle','1:','root','htb'])
    for speedclass in SPEEDCLASSES:
        #Now we add a class for each speedclass
        subprocess.check_call(['tc','class','add','dev',iface,'parent','1:','classid',speedclass[1],'htb','rate',str(speedclass[0])+'mbit'])
    #we'll do the filters when we actually have flows/subnets to filter
    return

def loadconnections(connections,filename,json,timeout,verbose):
    '''oversees pushing the given connections into the database'''
    #Sentinel representing invalid result is -1
    #This is a vestige of an old organization: this db connection should be made once in doconns and the cursor should be passed around.
    conn = sqlite3.connect(filename)
    c = conn.cursor()
    numnew,numupdated = 0,0
    for connection in connections:
        ips, ports, mss, rtt, wscaleavg, cwnd, unacked, retrans, lost, tcp = parseconnection(connection)
        if mss<0:
            logging.warning(connection+' had an invalid mss')
            continue
        if rtt<0:
            logging.warning(connection+' had an invalid rtt.')
            continue
        if wscaleavg<0:
            logging.warning(connection+' had an invalid wscaleavg.')
            continue
        if cwnd<0:
            logging.warning(connection+' had an invalid maxcwnd.')
            continue
        if retrans<0:
            #see if parsetcp will handle it--that's why there's no continue here
            logging.warning(connection+' had an invalid retrans reported by ss.')
        iface = findiface(ips[1])
        try:
            #the last 2 arguments are interval and flownum, respectively
            #interval is incremented every time the row is touched--we're assumuing this connection has never been seen before, so we leave it at 0
            #flownum allows us to take flows that have the same TCP 4-tuple, and tell them apart based on a delay (timeout) from the last time they were seen. This addresses the issue of multiple consecutive flows with the same 4-tuple being seen as a single, huge flow.
            dbinsert(c,ips[0],ips[1],ports[0],ports[1],mss,rtt,wscaleavg,cwnd,unacked,retrans,lost,tcp,0,iface,0,0)
            numnew +=1
        except sqlite3.IntegrityError:
            #We already have it in the database
            #I'm looking for a way to do this in one query in SQLite
            #Check if we grabbed this flow's data in the last couple of intervals or so...
            flownum,recent = dbcheckrecent(c,ips[0],ips[1],ports[0],ports[1],timeout)
            #If we haven't seen it recently, just create a new row with a higher flownumber
            if not recent:
                flownum += 1
                dbinsert(c,ips[0],ips[1],ports[0],ports[1],mss,rtt,wscaleavg,cwnd,unacked,retrans,lost,tcp,0,iface,0,flownum)
                numnew += 1
                #this is when we want to dump the previous flow's data, for now.
                #but we need a way to do this that doesn't depend on seeing the same flow twice.
                #if json:
                #    dumplast(sourceip,destip,sourceport,destport,flownum-1,json)
            else:
                #just update the appropriate values in the database row
                intervals = int(dbselectval(c,ips[0],ips[1],ports[0],ports[1],'intervals'))
                intervals += 1
                sumcwnd = int(dbselectval(c,ips[0],ips[1],ports[0],ports[1],'sumcwnd'))
                sumcwnd += int(cwnd)
                maxcwnd = int(dbselectval(c,ips[0],ips[1],ports[0],ports[1],'maxcwnd'))
                if cwnd > maxcwnd:
                    maxcwnd = cwnd
                #we are trying to gather the minimum reported average RTT:
                oldrtt = int(dbselectval(c,ips[0],ips[1],ports[0],ports[1],'rttavg'))
                if 0<oldrtt<rtt:
                    rtt = oldrtt
                #everything else gets updated to the latest values.
                dbupdateconn(c,ips[0],ips[1],ports[0],ports[1],mss,rtt,wscaleavg,maxcwnd,unacked,retrans,lost,tcp,sumcwnd,iface,intervals,flownum)
                numupdated += 1
    conn.commit()
    conn.close()
    logging.info('{numn} new connections loaded and {numu} connections updated at time {when}'.format(numn=numnew, numu=numupdated,
        when=datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%s')))
    return

def dumplast(sourceip,destip,sourceport,destport,flownum,json):
    '''json dump of last completed connection'''
    pass

def dbcheckrecent(cur, sourceip, destip, sourceport, destport, timeout):
    '''checks to see if this flow has been seen within timeout'''
    #we naively check the flow with the highest flownum
    query = '''SELECT flownum FROM conns WHERE
        sourceip = \'{sip}\' AND
        destip = \'{dip}\' AND
        sourceport = {spo} AND
        destport = {dpo} AND
        strftime('%s', datetime('now')) - strftime('%s', modified) <= {to}
        ORDER BY flownum DESC LIMIT 1'''.format(
            sip=sourceip,
            dip=destip,
            spo=sourceport,
            dpo=destport,
            to=timeout)
    cur.execute(query)
    out = cur.fetchall()
    if len(out)>0:
        return int(out[0][0]), True
    else:
        return int(dbselectval(cur, sourceip, destip, sourceport, destport, 'flownum')), False

def isip6(ip):
    '''determines if an ip address is v4 or v6'''
    try:
        socket.inet_aton(ip)
        return False
    except socket.error:
        try:
            socket.inet_pton(socket.AF_INET6,ip)
            return True
        except socket.error:
            return -1

def findiface(ip):
    '''determines the interface responsible for a particular ip address'''
    ip6 = isip6(ip)
    if ip6 == -1:
        #propagate failure to caller--we don't even know what ip call to make
        return -1
    elif ip6:
        dev = subprocess.check_output(['ip','-6','route','get',ip])
        dev = re.search('dev\s+\S+',dev).group(0).split()[1]
        return dev
    else:
        dev = subprocess.check_output(['ip','route','get',ip])
        dev = re.search('dev\s+\S+',dev).group(0).split()[1]
        return dev

def parseconnection(connection):
    '''parses a string representing a single TCP connection'''
    #Junk gets filtered in @loadconnections
    #try:
    connection = connection.strip()
    ordered = re.sub(':|,|/|Mbps',' ',connection)
    ordered = connection.split()
    ips = re.findall('\d+\.\d+\.\d+\.\d+',connection)
    ports = re.findall('\d:\w+',connection)
    mss = re.search('mss:\d+',connection)
    rtt = re.search('rtt:\d+[.]?\d+',connection)
    tcp = re.search('\S+\sw',connection)
    wscaleavg = re.search('wscale:\d+',connection)
    maxcwnd = re.search('cwnd:\d+',connection)
    unacked = re.search('unacked:\d+',connection)
    retrans = re.search('retrans:\d+\/\d+',connection)
    lost = re.search('lost:\d+',connection)
    #except Exception as e:
    #    if TEST or DEBUG:
    #        raise e
    #    logging.warning('connection {} could not be parsed'.format(connection))
    #    return -1,-1,-1,-1,-1,-1,-1,-1,-1,-1
    if mss:
        mss = int(mss.group(0)[4:])
    else:
        mss = -1
    if tcp:
        tcp = tcp.group(0)[:-2]
    else:
        tcp = -1
    if rtt:
        rtt = float(rtt.group(0)[4:])
    else:
        rtt = -1
    if wscaleavg:
        wscaleavg = wscaleavg.group(0)[7:]
    else:
        wscaleavg = -1
    if maxcwnd:
        maxcwnd = float(maxcwnd.group(0)[5:])
    else:
        maxcwnd = -1
    if unacked:
        unacked = int(unacked.group(0)[8:])
    else:
        unacked = -1
    if retrans:
        retrans = retrans.group(0)
        retrans = re.sub('retrans:\d+\/','',retrans)
    else:
        retrans = -1
    if lost:
        lost = int(lost.group(0)[5:])
    else:
        lost = -1
    if len(ips) > 1 and len(ports) > 1:
        ports[0] = ports[0][2:]
        ports[1] = ports[1][2:]
        return ips, ports, mss, rtt, wscaleavg, maxcwnd, unacked, retrans, lost, tcp
    logging.warning('connection {} could not be parsed'.format(connection))
    return -1,-1,-1,-1,-1,-1,-1,-1,-1,-1

def dbinsert(cur, sourceip, destip, sourceport, destport, mss, rtt, wscaleavg, maxcwnd, unacked, retrans, lost, tcp, sumcwnd, iface, intervals, flownum):
    '''assembles a query and creates a corresponding row in the database'''
    query = '''INSERT INTO conns (
        sourceip,
        destip,
        sourceport,
        destport,
        flownum,
        iface,
        mss,
        rttavg,
        wscaleavg,
        maxcwnd,
        unacked,
        retrans,
        lost,
        tcp,
        sumcwnd,
        intervals,
        created,
        modified)
    VALUES(
            \'{sip}\',
            \'{dip}\',
            {spo},
            {dpo},
            {fnm},
            \'{ifa}\',
            {ms},
            {rt},
            {wsc},
            {cnd},
            {una},
            {retr},
            {lst},
            \'{tp}\',
            {scnd},
            {intv},
            datetime(CURRENT_TIMESTAMP),
            datetime(CURRENT_TIMESTAMP))'''.format(
            sip=sourceip,
            dip=destip,
            spo=sourceport,
            dpo=destport,
            fnm=flownum,
            ifa=iface,
            ms=mss,
            rt=rtt,
            wsc=wscaleavg,
            cnd=maxcwnd,
            una=unacked,
            retr=retrans,
            lst=lost,
            tp=tcp,
            scnd=sumcwnd,
            intv=intervals)
    cur.execute(query)
    return

def dbupdateconn(cur, sourceip, destip, sourceport, destport, mss, rtt, wscaleavg, maxcwnd, unacked, retrans, lost, tcp, sumcwnd, iface, intervals, flownum):
    '''assembles a query and updates the entire corresponding row in the database'''
    if retrans == -1: #don't update it: let parsetcp take care of it.
        query = '''UPDATE conns SET
        iface = \'{ifa}\',
        mss = {ms},
        rttavg = {rt},
        wscaleavg = {wsc},
        maxcwnd = {cnd},
        unacked = {una},
        lost = {lst},
        tcp = \'{tcp}\',
        sumcwnd = {scnd},
        intervals = {intv},
        modified = datetime(CURRENT_TIMESTAMP)
        WHERE
        sourceip = \'{sip}\' AND
        destip = \'{dip}\' AND
        sourceport = {spo} AND
        destport = {dpo} AND
        flownum = {fnm}'''.format(
            sip=sourceip,
            dip=destip,
            spo=sourceport,
            dpo=destport,
            fnm=flownum,
            ifa=iface,
            ms=mss,
            rt=rtt,
            wsc=wscaleavg,
            cnd=maxcwnd,
            una=unacked,
            lst=lost,
            tcp=tcp,
            scnd=sumcwnd,
            intv=intervals)
    else:
        query = '''UPDATE conns SET
        iface = \'{ifa}\',
        mss = {ms},
        rttavg = {rt},
        wscaleavg = {wsc},
        maxcwnd = {cnd},
        unacked = {una},
        retrans = {retr},
        lost = {lst},
        tcp = \'{tcp}\',
        sumcwnd = {scnd},
        intervals = {intv},
        modified = datetime(CURRENT_TIMESTAMP)
        WHERE
        sourceip = \'{sip}\' AND
        destip = \'{dip}\' AND
        sourceport = {spo} AND
        destport = {dpo} AND
        flownum = {fnm}'''.format(
            sip=sourceip,
            dip=destip,
            spo=sourceport,
            dpo=destport,
            fnm=flownum,
            ifa=iface,
            ms=mss,
            rt=rtt,
            wsc=wscaleavg,
            cnd=maxcwnd,
            una=unacked,
            retr=retrans,
            lst=lost,
            tcp=tcp,
            scnd=sumcwnd,
            intv=intervals)
    cur.execute(query)
    return

def dbselectval(cur, sourceip, destip, sourceport, destport, selectfield):
    '''returns the \'latest\' particular value from the database'''
    query = '''SELECT {sval} FROM conns WHERE
    sourceip = \'{sip}\' AND
    destip = \'{dip}\' AND
    sourceport = {spo} AND
    destport = {dpo} ORDER BY flownum DESC LIMIT 1'''.format(
        sval=selectfield,
        sip=sourceip,
        dip=destip,
        spo=sourceport,
        dpo=destport)
    cur.execute(query)
    out = cur.fetchall()
    if len(out)>0:
        return out[0][0]
    return -1

def dbselectall(cur, sourceip, destip, sourceport, destport, flownum):
    '''returns the entire given row from the database'''
    query = '''SELECT * FROM conns WHERE
    sourceip = \'{sip}\' AND
    destip = \'{dip}\' AND
    sourceport = {spo} AND
    destport = {dpo} AND
    flownum = {fnm}'''.format(
        sval=selectfield,
        sip=sourceip,
        dip=destip,
        spo=sourceport,
        dpo=destport,
        fnm=flownum)
    cur.execute(query)
    out = cur.fetchall()
    return out

def dbupdateval(cur, sourceip, destip, sourceport, destport, updatefield, updateval):
    '''updates a particular value in the database'''
    if type(updateval) == str:
        updateval = '\''+updateval+'\''
    query = '''UPDATE conns SET {ufield}={uval} WHERE
        sourceip=\'{sip}\' AND
        sourceport={spo} AND
        destip=\'{dip}\' AND
        destport={dpo}'''.format(
            ufield=updatefield,
            uval=updateval,
            sip=str(sourceip),
            spo=sourceport,
            dip=str(destip),
            dpo=destport)
    cur.execute(query)
    return

def dbinit(filename):
    '''initializes the database and creates the table, if one doesn't exist already'''
    conn = sqlite3.connect(filename)
    c = conn.cursor()
    try:
        #see if it's already there...
        c.execute('''SELECT * FROM conns''')
    except sqlite3.OperationalError:
        logging.warning('Table doesn\'t exist; Creating table...')
        c.execute('''CREATE TABLE conns (
            sourceip    text    NOT NULL,
            destip      text    NOT NULL,
            sourceport  int     NOT NULL,
            destport    int     NOT NULL,
            flownum     int     NOT NULL,
            rxq         int,
            txq         int,
            mss         int,
            iface       text,
            rttavg      real,
            wscaleavg   real,
            maxcwnd     int,
            unacked     int,
            retrans     int,
            lost        int,
            tcp         text,
            sumcwnd     real,
            intervals   int,
            created     datetime,
            modified    datetime,
            PRIMARY KEY (sourceip, sourceport, destip, destport, flownum));''')
    conn.commit()
    conn.close()

def throttleoutgoing(iface,ipaddr,speedclass):
    '''throttles an outgoing flow'''
    #Need a decent rx linerate estimate in order to do this; became skeptical of tc's sendrate
    success = subprocess.check_call(['tc','filter','add',iface,'parent','1:','protocol','ip','prio','1','u32','match','ip','dst',ipaddr+'/32','flowid',speedclass[1]])
    return success

def pollss():
    '''gets data from ss'''
    #ss is being used to get statistics about tcp flows.
    #however, it doesn't always approprately report retransmits, so sometimes...
    out = subprocess.check_output(['ss','-i','-t','-n'])
    out = re.sub('\A.+\n','',out)
    out = re.sub('\n\t','',out)
    out = out.splitlines()
    return out

def polltcp():
    '''gets data from /proc/net/tcp'''
    #...we have to access /proc/net/tcp
    tcp = open('/proc/net/tcp','r')
    out = tcp.readlines()
    out = out[1:]
    tcp.close()
    #IPv6 support
    #tcp6 = open('/proc/net/tcp6','r')
    #out6 = tcp6.readlines()
    #out6 = out6[1:]
    #tcp6.close()
    #out += out6
    return out

def parsetcp(connections,filename):
    '''parses the data from /proc/net/tcp'''
    conn = sqlite3.connect(filename)
    c = conn.cursor()
    for connection in connections:
        connection = connection.strip()
        connection = connection.split()
        #con't care about localhost connections
        if connection[1] != '00000000:0000' and connection[2] != '00000000:0000':
            sourceip = connection[1].split(':')[0]
            sourceport = connection[1].split(':')[1]
            #have to convert the packed hex to a valid IP address
            sourceip = int(sourceip,16)
            sourceip = struct.pack('<L',sourceip)
            sourceip = socket.inet_ntoa(sourceip)
            sourceport = int(sourceport,16)
            destip = connection[2].split(':')[0]
            destport = connection[2].split(':')[1]
            destip = int(destip,16)
            destip = struct.pack('<L',destip)
            destip = socket.inet_ntoa(destip)
            destport = int(destport,16)
            retrans = int(connection[6],16)
            tempretr = int(dbselectval(c,sourceip,destip,sourceport,destport,'retrans'))
            if tempretr > 0:
                retrans += tempretr
            dbupdateval(c,sourceip, destip, sourceport, destport,'retrans',retrans)
    conn.commit()
    conn.close()

def doconns(interval,filename,json,timeout,verbose):
    '''manages the periodic collection of ss and procfs data'''
    connections = pollss()
    tcpconns = polltcp()
    loadconnections(connections,filename,json,timeout,verbose)
    parsetcp(tcpconns,filename)
    logging.info('successful interval')
    threading.Timer(interval, doconns, [interval,filename,json,timeout,verbose]).start()
    return

def main(argv=None): # IGNORE:C0111
    '''Command line options.'''
    if DEBUG:
        _level = logging.DEBUG
    else:
        _level = logging.INFO
    logging.basicConfig(filename='monitor.log',format='%(asctime)s %(message)s',level=_level)
    #Setting defaults
    filename = 'connections.db'
    json = None
    timeout = 30
    verbose=False
    logging.info('Configured and running.')
    if argv is None:
        argv = sys.argv
    else:
        sys.argv.extend(argv)
    program_name = os.path.basename(sys.argv[0])
    program_version = 'v{}'.format(__version__)
    program_build_date = str(__updated__)
    program_version_message = '%%(prog)s {v} ({b})'.format(v=program_version, b=program_build_date)
    program_shortdesc = __import__('__main__').__doc__.split('\n')[1]
    program_license = '''{}



USAGE
'''.format(program_shortdesc)

    try:
        # Setup argument parser
        parser = argparse.ArgumentParser(description=program_license, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('interval',
            metavar='interval',
            type=int,
            action='store',
            help='Specify the monitoring interval in seconds. (min: 1, max: 60)')
        parser.add_argument('-f','--filename',
            dest='filename',
            metavar='filename',
            action='store',
            help='Specify the filename/location of your SQLite database.')
        parser.add_argument('-j','--json',
            dest='json',
            metavar='json',
            action='store',
            help='Future: Specify the folder where you would like to write out the JSON BLOB(s).')
        parser.add_argument('-i','--interface',
            dest='interface',
            metavar='interface',
            action='store',
            help='Future: Specify the name of the interface you wish to monitor/throttle.')
        parser.add_argument('-t','--timeout',
            dest='timeout',
            metavar='timeout',
            type=int,
            action='store',
            help='''Specify the amount of time that will pass in seconds before connections
            with the same ports and destination are considered new.''')
        parser.add_argument('-a','--affinitize',
            action='store_true',
            help='Affinitize and optimize the system. Must supply an interface.')
        parser.add_argument('-r','--throttle',
            action='store_true',
            help='Future: Actively shape connections, rather than just collecting data.')
        parser.add_argument('-v','--verbose',
            action='store_true',
            help='Don\'t summarize connections in the database.')
        # Process arguments
        args = parser.parse_args()
        if not 0<args.interval<=60:
            raise CLIError('minimum interval is 1 second, maximum is 60 seconds')
        interval = args.interval
        if args.throttle and not args.interface:
            raise CLIError('Please supply an interface to throttle with the -i option.')
        if args.affinitize and not args.interface:
            raise CLIError('Please supply an interface to affinitize with the -i option.')
        if args.timeout:
            timeout = args.timeout
        if timeout < interval:
            raise CLIError('Timeout cannot be less than an interval.')
    except KeyboardInterrupt:
        print 'Operation Cancelled\n'
        return 0
    except Exception as e:
        if DEBUG or TESTRUN:
            print 'debug or testrun was set'
            raise(e)
        indent = len(program_name) * ' '
        sys.stderr.write(program_name + ': ' + repr(e) + '\n')
        sys.stderr.write(indent + '  for help use --help'+'\n')
        return 2
    if args.throttle:
        throttle = True
    if args.interface:
        interface = args.interface
    if args.filename:
        filename = args.filename
    if args.json:
        if os.path.isdir(json):
            json = args.json
        else:
            raise CLIError('Please enter a valid directory for JSON output.')
    if args.timeout:
        timeout = args.timeout
    if args.affinitize:
        affinitize = True
    if args.verbose:
        verbose = True
    dbinit(filename)
    if args.affinitize:
        checkibalance()
        numcpus = pollcpu()
        print 'The number of cpus is:', numcpus
        irqlist = pollirq(interface)
        affinity = pollaffinity(irqlist)
        print affinity
        setaffinity(affinity,numcpus)
    if args.interface:
        linerate = getlinerate(args.interface)
    if args.throttle:
        setthrottles(interface)
    doconns(interval,filename,json,timeout,verbose)
    logging.shutdown()

if __name__ == '__main__':
    if DEBUG:
        sys.argv.append('-h')
    if TESTRUN:
        import doctest
        doctest.testmod()
    sys.exit(main())
