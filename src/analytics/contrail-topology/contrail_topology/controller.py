#
# Copyright (c) 2015 Juniper Networks, Inc. All rights reserved.
#
from analytic_client import AnalyticApiClient
import time, socket, os
from topology_uve import LinkUve
import gevent
from gevent.coros import Semaphore
from opserver.consistent_schdlr import ConsistentScheduler

class PRouter(object):
    def __init__(self, name, data):
        self.name = name
        self.data = data

class Controller(object):
    def __init__(self, config):
        self._config = config
        self._me = socket.gethostname() + ':' + str(os.getpid())
        self.analytic_api = AnalyticApiClient(self._config)
        self.uve = LinkUve(self._config)
        self.sleep_time()
        self._keep_running = True

    def stop(self):
        self._keep_running = False

    def sleep_time(self, newtime=None):
        if newtime:
            self._sleep_time = newtime
        else:
            self._sleep_time = self._config.frequency()
        return self._sleep_time

    def get_vrouters(self):
        self.analytic_api.get_vrouters(True)
        self.vrouters = {}
        self.vrouter_ips = {}
        self.vrouter_macs = {}
        for vr in self.analytic_api.list_vrouters():
            d = self.analytic_api.get_vrouter(vr, 'VrouterAgent:phy_if')
            if 'VrouterAgent' not in d:
                d['VrouterAgent'] = {}
            _ipl = self.analytic_api.get_vrouter(vr,
                    'VrouterAgent:self_ip_list')
            if 'VrouterAgent' in _ipl:
                d['VrouterAgent'].update(_ipl['VrouterAgent'])
            if 'VrouterAgent' not in d or\
                'self_ip_list' not in d['VrouterAgent'] or\
                'phy_if' not in d['VrouterAgent']:
                continue
            self.vrouters[vr] = {'ips': d['VrouterAgent']['self_ip_list'],
                'if': d['VrouterAgent']['phy_if'],
            }
            for ip in d['VrouterAgent']['self_ip_list']:
                self.vrouter_ips[ip] = vr # index
            for intf in d['VrouterAgent']['phy_if']:
                try:
                    self.vrouter_macs[intf['mac_address']] = {}
                    self.vrouter_macs[intf['mac_address']]['vrname'] = vr
                    self.vrouter_macs[intf['mac_address']]['ifname'] = intf['name']
                except:
                    continue

    def get_prouters(self):
        self.analytic_api.get_prouters(True)
        self.prouters = []
        for pr in self.analytic_api.list_prouters():
            self.prouters.append(PRouter(pr, self.analytic_api.get_prouter(
                            pr, 'PRouterEntry')))

    def _is_linkup(self, prouter, ifindex):
        if 'PRouterEntry' in prouter.data and \
            'ifIndexOperStatusTable' in prouter.data['PRouterEntry']:
                status = filter(lambda x: x['ifIndex'] == ifindex,
                        prouter.data['PRouterEntry']['ifIndexOperStatusTable'])
                if status and status[0]['ifOperStatus'] == 1:
                    return True
        return False

    def _add_link(self, prouter, remote_system_name, local_interface_name,
                  remote_interface_name, local_interface_index,
                  remote_interface_index, link_type):
        d = dict(remote_system_name=remote_system_name,
                 local_interface_name=local_interface_name,
                 remote_interface_name=remote_interface_name,
                 local_interface_index=local_interface_index,
                 remote_interface_index=remote_interface_index,
                 type=link_type)
        if self._is_linkup(prouter, local_interface_index):
            if prouter.name in self.link:
                self.link[prouter.name].append(d)
            else:
                self.link[prouter.name] = [d]
            return True
        return False

    def compute(self):
        self.link = {}
        for prouter in self.constnt_schdlr.work_items():
            pr, d = prouter.name, prouter.data
            if 'PRouterEntry' not in d or 'ifTable' not in d['PRouterEntry']:
                continue
            self.link[pr] = []
            lldp_ints = []
            ifm = dict(map(lambda x: (x['ifIndex'], x['ifDescr']),
                        d['PRouterEntry']['ifTable']))
            for pl in d['PRouterEntry']['lldpTable']['lldpRemoteSystemsData']:
                if pl['lldpRemLocalPortNum'] in ifm:
                    if pl['lldpRemPortId'].isdigit():
                        rii = int(pl['lldpRemPortId'])
                    else:
                        try:
                            rii = filter(lambda y: y['ifName'] == pl[
                                    'lldpRemPortId'], [ x for x in self.prouters \
                                    if x.name == pl['lldpRemSysName']][0].data[
                                    'PRouterEntry']['ifXTable'])[0]['ifIndex']
                        except:
                            rii = 0

                    if self._add_link(
                            prouter=prouter,
                            remote_system_name=pl['lldpRemSysName'],
                            local_interface_name=ifm[pl['lldpRemLocalPortNum']],
                            remote_interface_name=pl['lldpRemPortDesc'],
                            local_interface_index=pl['lldpRemLocalPortNum'],
                            remote_interface_index=rii,
                            link_type=1):
                        lldp_ints.append(ifm[pl['lldpRemLocalPortNum']])

            vrouter_neighbors = []
            if 'fdbPortIfIndexTable' in d['PRouterEntry']:
                dot1d2snmp = map (lambda x: (
                            x['dot1dBasePortIfIndex'],
                            x['snmpIfIndex']),
                        d['PRouterEntry']['fdbPortIfIndexTable'])
                dot1d2snmp_dict = dict(dot1d2snmp)
                if 'fdbPortTable' in d['PRouterEntry']:
                    for mac_entry in d['PRouterEntry']['fdbPortTable']:
                        if mac_entry['mac'] in self.vrouter_macs:
                            vrouter_mac_entry = self.vrouter_macs[mac_entry['mac']]
                            fdbport = mac_entry['dot1dBasePortIfIndex']
                            try:
                                snmpport = dot1d2snmp_dict[fdbport]
                                ifname = ifm[snmpport]
                            except:
                                continue
                            is_lldp_int = any(ifname == lldp_int for lldp_int in lldp_ints)
                            if is_lldp_int:
                                continue
                            if self._add_link(
                                    prouter=prouter,
                                    remote_system_name=vrouter_mac_entry['vrname'],
                                    local_interface_name=ifname,
                                    remote_interface_name=vrouter_mac_entry[
                                                'ifname'],
                                    local_interface_index=snmpport,
                                    remote_interface_index=1, #dont know TODO:FIX
                                    link_type=2):
                                vrouter_neighbors.append(
                                        vrouter_mac_entry['vrname'])
            for arp in d['PRouterEntry']['arpTable']:
                if arp['ip'] in self.vrouter_ips:
                    if arp['mac'] in map(lambda x: x['mac_address'],
                            self.vrouters[self.vrouter_ips[arp['ip']]]['if']):
                        vr_name = arp['ip']
                        vr = self.vrouters[self.vrouter_ips[vr_name]]
                        if self.vrouter_ips[vr_name] in vrouter_neighbors:
                            continue
                        if ifm[arp['localIfIndex']].startswith('vlan'):
                            continue
                        if ifm[arp['localIfIndex']].startswith('irb'):
                            continue
                        is_lldp_int = any(ifm[arp['localIfIndex']] == lldp_int for lldp_int in lldp_ints)
                        if is_lldp_int:
                            continue
                        if self._add_link(
                                prouter=prouter,
                                remote_system_name=self.vrouter_ips[vr_name],
                                local_interface_name=ifm[arp['localIfIndex']],
                                remote_interface_name=vr['if'][-1]['name'],#TODO
                                local_interface_index=arp['localIfIndex'],
                                remote_interface_index=1, #dont know TODO:FIX
                                link_type=2):
                            pass

    def send_uve(self):
        self.uve.send(self.link)

    def switcher(self):
        gevent.sleep(0)

    def scan_data(self):
        t = []
        t.append(gevent.spawn(self.get_vrouters))
        t.append(gevent.spawn(self.get_prouters))
        gevent.joinall(t)

    def _del_uves(self, prouters):
        with self._sem:
            for prouter in prouters:
                self.uve.delete(prouter.name)

    def run(self):
        self._sem = Semaphore()
        self.constnt_schdlr = ConsistentScheduler(
                            self.uve._moduleid,
                            zookeeper=self._config.zookeeper_server(),
                            delete_hndlr=self._del_uves)
        while self._keep_running:
            self.scan_data()
            if self.constnt_schdlr.schedule(self.prouters):
                try:
                    with self._sem:
                        self.compute()
                        self.send_uve()
                except Exception as e:
                    import traceback; traceback.print_exc()
                    print str(e)
                gevent.sleep(self._sleep_time)
            else:
                gevent.sleep(1)
        self.constnt_schdlr.finish()
