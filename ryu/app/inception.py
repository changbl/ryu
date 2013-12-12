"""
Inception Cloud SDN controller
"""

from collections import defaultdict
import logging

from oslo.config import cfg

from ryu.base import app_manager
from ryu.controller import dpset
from ryu.controller import network
from ryu.controller import ofp_event
from ryu.controller import handler
from ryu.ofproto import ofproto_v1_2
from ryu.ofproto import ether
from ryu.ofproto import inet
from ryu.lib import mac
from ryu.lib.dpid import dpid_to_str
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.app.inception_arp import InceptionArp
from ryu.app.inception_dhcp import InceptionDhcp
from ryu.app.inception_dhcp import DHCP_CLIENT_PORT
from ryu.app.inception_dhcp import DHCP_SERVER_PORT
import ryu.app.inception_priority as priority

LOGGER = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.register_opts([
    cfg.StrOpt('ip_prefix',
               default='192.168',
               help="X1.X2 in your network's IP address X1.X2.X3.X4")])


class Inception(app_manager.RyuApp):
    """
    Inception Cloud SDN controller
    """
    _CONTEXTS = {
        'network': network.Network,
        'dpset': dpset.DPSet
    }
    OFP_VERSIONS = [ofproto_v1_2.OFP_VERSION]

    MAX_LEN = 65535

    def __init__(self, *args, **kwargs):
        super(Inception, self).__init__(*args, **kwargs)

        self.ip_prefix = CONF.ip_prefix

        # dpset: management of all connected switches
        self.dpset = kwargs['dpset']

        # network: port information of switches
        self.network = kwargs['network']

        # {dpid -> IP address}:
        # Records the "IP address" of rVM where a switch ("dpid") resides.
        self.dpid_to_ip = {}

        # {dpid -> {IP address -> port}}:
        # Records the neighboring relations of each switch.
        # "IP address": address of remote VMs
        # "port": port number of dpid connecting IP address
        self.dpid_to_conns = defaultdict(dict)

        # {MAC => (dpid, port)}:
        # Records the switch ("dpid") to which a local "mac" connects,
        # as well as the "port" of the connection.
        self.mac_to_dpid_port = {}

        # [(dpid, mac)]:
        # "dpid" installed a rule forwarding packets to a "mac"
        self.unicast_flows = []

        ## Modules
        # ARP
        self.inception_arp = InceptionArp(self)
        # DHCP
        self.inception_dhcp = InceptionDhcp(self)

    @handler.set_ev_cls(dpset.EventDP, dpset.DPSET_EV_DISPATCHER)
    def switch_connection_handler(self, event):
        """
        Handle when a switch event is received
        """
        datapath = event.dp
        dpid = datapath.id
        ofproto = datapath.ofproto
        ofproto_parser = datapath.ofproto_parser

        # A new switch connects
        if event.enter is True:
            socket = datapath.socket
            ip, port = socket.getpeername()
            host_ports = []
            all_ports = []

            # If the entry corresponding to the MAC already exists
            if dpid in self.dpid_to_ip:
                LOGGER.info("(switch=%s) already updated", dpid_to_str(dpid))
                return

            self.dpid_to_ip[dpid] = ip
            LOGGER.info("Add: (switch=%s) -> (ip=%s)", dpid_to_str(dpid), ip)

            # Collect port information.  Sift out ports connecting peer
            # switches and store them in dpid_to_conns
            for port in event.ports:
                # TODO(changbl): Use OVSDB. Parse the port name to get the IP
                # address of remote rVM to which the bridge builds a
                # VXLAN. E.g., obr1_184-53 => ip_prefix.184.53. Only
                # store the port connecting remote rVM.
                port_no = port.port_no
                all_ports.append(port_no)
                if port.name.startswith('obr') and '_' in port.name:
                    _, ip_suffix = port.name.split('_')
                    ip_suffix = ip_suffix.replace('-', '.')
                    peer_ip = '.'.join((self.ip_prefix, ip_suffix))
                    self.dpid_to_conns[dpid][peer_ip] = port_no
                    LOGGER.info("Add: (switch=%s, peer_ip=%s) -> (port=%s)",
                                dpid_to_str(dpid), peer_ip, port_no)
                elif port.name == 'eth_dhcp':
                    self.inception_dhcp.update_server(dpid, port_no)
                    LOGGER.info("DHCP server is found!")
                else:
                    # Store the port connecting local hosts
                    host_ports.append(port_no)

            # Set up one flow for ARP messages
            # Intercepts all ARP packets and send them to the controller
            actions_arp = [ofproto_parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER, Inception.MAX_LEN)]
            instruction_arp = [datapath.ofproto_parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions_arp)]
            datapath.send_msg(ofproto_parser.OFPFlowMod(
                datapath=datapath,
                match=ofproto_parser.OFPMatch(
                    eth_type=ether.ETH_TYPE_ARP,
                ),
                priority=priority.ARP,
                flags=ofproto.OFPFF_SEND_FLOW_REM,
                cookie=0,
                command=ofproto.OFPFC_ADD,
                instructions=instruction_arp
            ))

            # Set up two flows for DHCP messages
            # (1) Intercept all DHCP request packets and send to the controller
            actions_dhcp = [ofproto_parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER, Inception.MAX_LEN)]
            instruction_dhcp = [datapath.ofproto_parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions_dhcp)]
            datapath.send_msg(ofproto_parser.OFPFlowMod(
                datapath=datapath,
                match=ofproto_parser.OFPMatch(
                    eth_type=ether.ETH_TYPE_IP,
                    ip_proto=inet.IPPROTO_UDP,
                    udp_src=DHCP_CLIENT_PORT,
                ),
                priority=priority.DHCP,
                flags=ofproto.OFPFF_SEND_FLOW_REM,
                cookie=0,
                command=ofproto.OFPFC_ADD,
                instructions=instruction_dhcp
            ))
            # (2) Intercept all DHCP reply packets and send to the controller
            actions_dhcp = [ofproto_parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER, Inception.MAX_LEN)]
            instruction_dhcp = [datapath.ofproto_parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions_dhcp)]
            datapath.send_msg(ofproto_parser.OFPFlowMod(
                datapath=datapath,
                match=ofproto_parser.OFPMatch(
                    eth_type=ether.ETH_TYPE_IP,
                    ip_proto=inet.IPPROTO_UDP,
                    udp_src=DHCP_SERVER_PORT,
                ),
                priority=priority.DHCP,
                flags=ofproto.OFPFF_SEND_FLOW_REM,
                cookie=0,
                command=ofproto.OFPFC_ADD,
                instructions=instruction_dhcp
            ))

            # Set up two parts of flows for broadcast messages
            # (1) Broadcast messages from each local port: forward to all
            # (other) ports
            for port_no in host_ports:
                actions_bcast_out = [ofproto_parser.OFPActionOutput(
                    ofproto.OFPP_ALL)]
                instructions_bcast_out = [datapath.ofproto_parser.
                    OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                        actions_bcast_out)]
                datapath.send_msg(ofproto_parser.OFPFlowMod(
                    datapath=datapath,
                    match=ofproto_parser.OFPMatch(
                        in_port=port_no,
                        eth_dst=mac.BROADCAST_STR,
                    ),
                    priority=priority.HOST_BCAST,
                    flags=ofproto.OFPFF_SEND_FLOW_REM,
                    cookie=0,
                    command=ofproto.OFPFC_ADD,
                    instructions=instructions_bcast_out
                ))
            # (2) Broadcast messages from each (tunnel) port: forward to all
            # local ports. Since priority.SWITCH_BCAST < priority.HOST_BCAST,
            # this guarantees that only tunnel-port message will trigger this
            # flow
            actions_bcast_in = [ofproto_parser.OFPActionOutput(port=port_no)
                                for port_no in host_ports]
            instruction_bcast_in = [datapath.ofproto_parser.
                OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                    actions_bcast_in)]
            datapath.send_msg(ofproto_parser.OFPFlowMod(
                datapath=datapath,
                match=ofproto_parser.OFPMatch(
                    eth_dst=mac.BROADCAST_STR,
                ),
                priority=priority.SWITCH_BCAST,
                flags=ofproto.OFPFF_SEND_FLOW_REM,
                cookie=0,
                command=ofproto.OFPFC_ADD,
                instructions=instruction_bcast_in
            ))

            # Finally, setup a default flow
            # Process via normal L2/L3 legacy switch configuration
            actions_norm = [ofproto_parser.
                OFPActionOutput(ofproto.OFPP_NORMAL)]
            instruction_norm = [datapath.ofproto_parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions_norm)]
            datapath.send_msg(ofproto_parser.OFPFlowMod(
                datapath=datapath,
                match=ofproto_parser.OFPMatch(),
                priority=priority.NORMAL,
                flags=ofproto.OFPFF_SEND_FLOW_REM,
                cookie=0,
                command=ofproto.OFPFC_ADD,
                instructions=instruction_norm
            ))

        # A switch disconnects
        else:
            LOGGER.info("Del: (switch=%s) -> (ip=%s)", dpid_to_str(dpid),
                        self.dpid_to_ip[dpid])
            # Delete switch's mapping from switch dpid to remote IP address
            del self.dpid_to_ip[dpid]

            # Delete the switch's connection info
            del self.dpid_to_conns[dpid]

            # Delete all connected hosts
            for mac_addr in self.mac_to_dpid_port.keys():
                local_dpid, _ = self.mac_to_dpid_port[mac_addr]
                if local_dpid == dpid:
                    del self.mac_to_dpid_port[mac_addr]

            # TODO(chenche): Delete all rules tracking

    @handler.set_ev_cls(ofp_event.EventOFPPacketIn, handler.MAIN_DISPATCHER)
    def packet_in_handler(self, event):
        """
        Handle when a packet is received
        """
        LOGGER.debug("Packet received")
        # do source learning
        self._do_source_learning(event)
        # handle ARP packet if it is
        self.inception_arp.handle(event)
        # handle DHCP packet if it is
        self.inception_dhcp.handle(event)

    def _do_source_learning(self, event):
        """
        Learn MAC => (switch dpid, switch port) mapping from a packet,
        update self.mac_to_dpid_port table. Also set up flow table for
        forwarding broadcast message
        """
        msg = event.msg
        datapath = msg.datapath
        dpid = datapath.id
        ofproto_parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        in_port = msg.match['in_port']

        whole_packet = packet.Packet(msg.data)
        ethernet_header = whole_packet.get_protocol(ethernet.ethernet)
        ethernet_src = ethernet_header.src
        if ethernet_src not in self.mac_to_dpid_port:
            self.mac_to_dpid_port[ethernet_src] = (dpid, in_port)
            LOGGER.info("Learn: (mac=%s) => (switch=%s, port=%s)", ethernet_src,
                        dpid_to_str(dpid), in_port)
            # Set unicast flow to ethernet_src
            actions_unicast = [ofproto_parser.OFPActionOutput(in_port)]
            instructions_unicast = [datapath.ofproto_parser.
                OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                      actions_unicast)]
            datapath.send_msg(ofproto_parser.OFPFlowMod(
                datapath=datapath,
                match=ofproto_parser.OFPMatch(
                    eth_dst=ethernet_src
                ),
                cookie=0,
                command=ofproto.OFPFC_ADD,
                priority=priority.DATA_FWD,
                flags=ofproto.OFPFF_SEND_FLOW_REM,
                instructions=instructions_unicast
            ))
            self.unicast_flows.append((dpid, ethernet_src))
            LOGGER.info("Setup local forward flow on (switch=%s) towards "
                        "(mac=%s)", dpid_to_str(dpid), ethernet_src)
        else:
            (dpid_record, in_port_record) = self.mac_to_dpid_port[ethernet_src]
            # The host's switch changes, e.g., due a VM live migration
            if dpid_record != dpid:
                datapath_record = self.dpset.get(dpid_record)
                ip_datapath = self.dpid_to_ip[dpid]
                self.mac_to_dpid_port[ethernet_src] = (dpid, in_port)
                LOGGER.info("Update: (mac=%s) => (switch=%s, port=%s)",
                            ethernet_src, dpid_to_str(dpid), in_port)

                # Add a flow on new datapath towards ethernet_src
                flow_command = None
                if (dpid, ethernet_src) in self.unicast_flows:
                    flow_command = ofproto.OFPFC_MODIFY_STRICT
                else:
                    flow_command = ofproto.OFPFC_ADD
                    self.unicast_flows.append((dpid, ethernet_src))
                actions_inport = [ofproto_parser.OFPActionOutput(in_port)]
                instructions_inport = [datapath.ofproto_parser.
                    OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                          actions_inport)]
                datapath.send_msg(ofproto_parser.OFPFlowMod(
                    datapath=datapath,
                    match=ofproto_parser.OFPMatch(
                        eth_dst=ethernet_src
                    ),
                    cookie=0,
                    command=flow_command,
                    priority=priority.DATA_FWD,
                    flags=ofproto.OFPFF_SEND_FLOW_REM,
                    instructions=instructions_inport
                ))
                LOGGER.info("Setup local forward flow on (switch=%s) towards "
                            "(mac=%s)", dpid_to_str(dpid), ethernet_src)

                # Mofidy flows on all other datapaths contacting ethernet_src
                for (remote_dpid, dst_mac) in self.unicast_flows:
                    if remote_dpid == dpid or dst_mac != ethernet_src:
                        continue

                    remote_datapath = self.dpset.get(remote_dpid)
                    remote_fwd_port = (self.dpid_to_conns[remote_dpid]
                                       [ip_datapath])
                    actions_remote = [ofproto_parser.OFPActionOutput(
                        remote_fwd_port)]
                    instructions_remote = [datapath.ofproto_parser.
                        OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                              actions_remote)]
                    remote_datapath.send_msg(ofproto_parser.OFPFlowMod(
                        datapath=remote_datapath,
                        match=ofproto_parser.OFPMatch(
                            eth_dst=ethernet_src
                        ),
                        cookie=0,
                        command=ofproto.OFPFC_MODIFY_STRICT,
                        priority=priority.DATA_FWD,
                        flags=ofproto.OFPFF_SEND_FLOW_REM,
                        instructions=instructions_remote
                    ))
                    LOGGER.info("Setup remote forward flow on (switch=%s) "
                                "towards (mac=%s)", dpid_to_str(remote_dpid),
                                ethernet_src)

    def setup_switch_fwd_flows(self, src_mac, dst_mac):
        """
        Given two MAC addresses, set up flows on their connected switches
        towards each other, so that the two can forward packets in between
        """
        (src_dpid, src_port) = self.mac_to_dpid_port[src_mac]
        (dst_dpid, dst_port) = self.mac_to_dpid_port[dst_mac]

        # If src_dpid == dst_dpid, no need to set up flows
        if src_dpid == dst_dpid:
            return

        src_ip = self.dpid_to_ip[src_dpid]
        dst_ip = self.dpid_to_ip[dst_dpid]
        src_fwd_port = self.dpid_to_conns[src_dpid][dst_ip]
        dst_fwd_port = self.dpid_to_conns[dst_dpid][src_ip]
        src_datapath = self.dpset.get(src_dpid)
        dst_datapath = self.dpset.get(dst_dpid)
        src_ofproto = src_datapath.ofproto
        dst_ofproto = dst_datapath.ofproto
        src_ofproto_parser = src_datapath.ofproto_parser
        dst_ofproto_parser = dst_datapath.ofproto_parser

        # Setup a flow on the src switch
        if (src_dpid, dst_mac) not in self.unicast_flows:
            actions_fwd = [src_ofproto_parser.OFPActionOutput(src_fwd_port)]
            instructions_fwd = [src_datapath.ofproto_parser.
                OFPInstructionActions(src_ofproto.OFPIT_APPLY_ACTIONS,
                                      actions_fwd)]
            src_datapath.send_msg(src_ofproto_parser.OFPFlowMod(
                datapath=src_datapath,
                match=src_ofproto_parser.OFPMatch(
                    eth_dst=dst_mac
                ),
                cookie=0,
                command=src_ofproto.OFPFC_ADD,
                priority=priority.DATA_FWD,
                flags=src_ofproto.OFPFF_SEND_FLOW_REM,
                instructions=instructions_fwd
            ))
            self.unicast_flows.append((src_dpid, dst_mac))
            LOGGER.info("Setup remote forward flow on (switch=%s) towards "
                        "(mac=%s)", dpid_to_str(src_dpid), dst_mac)

        # Setup a reverse flow on the dst switch
        if (dst_dpid, src_mac) not in self.unicast_flows:
            actions_dst = [dst_ofproto_parser.OFPActionOutput(dst_fwd_port)]
            instructions_dst = [dst_datapath.ofproto_parser.
                OFPInstructionActions(dst_ofproto.OFPIT_APPLY_ACTIONS,
                                      actions_dst)]
            dst_datapath.send_msg(dst_ofproto_parser.OFPFlowMod(
                datapath=dst_datapath,
                match=dst_ofproto_parser.OFPMatch(
                    eth_dst=src_mac
                ),
                cookie=0, command=dst_ofproto.OFPFC_ADD,
                priority=priority.DATA_FWD,
                flags=dst_ofproto.OFPFF_SEND_FLOW_REM,
                instructions=instructions_dst
                ))
            self.unicast_flows.append((dst_dpid, src_mac))
            LOGGER.info("Setup remote forward flow on (switch=%s) towards "
                        "(mac=%s)", dpid_to_str(dst_dpid), src_mac)