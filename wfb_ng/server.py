#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2018-2024 Vasily Evseenko <svpcom@p2ptech.org>

#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; version 3.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License along
#   with this program; if not, write to the Free Software Foundation, Inc.,
#   51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import sys
import msgpack
import os
import re
import hashlib
import time
import struct
import gzip

from itertools import groupby
from twisted.python import log, failure
from twisted.internet import reactor, defer, main as ti_main, threads, task
from twisted.internet.protocol import ProcessProtocol, Factory
from twisted.protocols.basic import LineReceiver, Int32StringReceiver
from twisted.internet.serialport import SerialPort

from . import _log_msg, ConsoleObserver, ErrorSafeLogFile, call_and_check_rc, ExecError
from .common import abort_on_crash, exit_status, df_sleep
from .config_parser import Section
from .proxy import UDPProxyProtocol, MavlinkSerialProxyProtocol, MavlinkUDPProxyProtocol
from .mavlink_protocol import MavlinkARMProtocol, MavlinkTCPFactory, MavlinkLoggerProtocol
from .tuntap import TUNTAPProtocol, TUNTAPTransport
from .conf import settings, cfg_files

connect_re = re.compile(r'^connect://(?P<addr>[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+):(?P<port>[0-9]+)$', re.IGNORECASE)
listen_re = re.compile(r'^listen://(?P<addr>[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+):(?P<port>[0-9]+)$', re.IGNORECASE)
serial_re = re.compile(r'^serial:(?P<dev>[a-z0-9\-\_/]+):(?P<baud>[0-9]+)$', re.IGNORECASE)


class BadTelemetry(Exception):
    pass


class WFBFlags(object):
    LINK_LOST = 1
    LINK_JAMMED = 2


fec_types = {1: 'VDM_RS'}


# Log format is gzipped sequence of int32 strings
# For every run new file will be open to avoid framing errors

def BinLogFile(self, fname, directory):
    filename = '%s.%s' % (fname, time.strftime('%Y%m%d-%H%M%S', time.localtime()))
    filename = os.path.join(directory, filename)
    reactor.callFromThread(log.msg, 'Open binary log %s' % (filename,))
    return gzip.GzipFile(filename, 'wb')


class BinLogger(ErrorSafeLogFile):
    binary = True
    twisted_logger = False
    flush_delay = 10
    log_cls = BinLogFile

    def send_stats(self, data):
        data = msgpack.packb(data)
        self.write(b''.join((struct.pack('!I', len(data)), data)))


class StatisticsProtocol(Int32StringReceiver):
    MAX_LENGTH = 1024 * 1024

    def connectionMade(self):
        self.sendString(msgpack.packb(dict(type='cli_title', cli_title=self.factory.cli_title)))
        self.factory.ui_sessions.append(self)

    def stringReceived(self, string):
        pass

    def connectionLost(self, reason):
        self.factory.ui_sessions.remove(self)

    def send_stats(self, data):
        self.sendString(msgpack.packb(data))


class StatsAndSelectorFactory(Factory):
    noisy = False
    protocol = StatisticsProtocol

    """
    Aggregate RX stats and select TX antenna
    """

    def __init__(self, profile, wlans, link_domain, logger):
        self.wlans = tuple(wlans)
        self.ant_sel_cb_list = []
        self.rssi_cb_l = []

        # Select antenna #0 by default
        self.tx_sel = 0
        self.tx_sel_rssi_delta = settings.common.tx_sel_rssi_delta
        self.tx_sel_counter_rel_delta = settings.common.tx_sel_counter_rel_delta
        self.tx_sel_counter_abs_delta = settings.common.tx_sel_counter_abs_delta

        # tcp sockets for UI
        self.ui_sessions = []

        # machine-readable logger
        self.logger = logger

        if logger is not None:
            logger.send_stats(dict(type='init',
                                   timestamp = time.time(),
                                   version=settings.common.version,
                                   profile=profile,
                                   wlans=wlans,
                                   link_domain=link_domain))

            self.ui_sessions.append(logger)

        # CLI title
        self.cli_title = 'WFB-ng_%s @%s %s [%s]' % (settings.common.version, profile, ', '.join(wlans), link_domain)

        # RF module temperature by rf_path
        self.rf_temperature = {}

        self.lc = task.LoopingCall(self.read_temperature)
        self.lc.start(settings.common.temp_measurement_interval, now=True)

    def _cleanup(self):
        self.lc.stop()

    def read_temperature(self):
        def _read_temperature():
            res = {}
            for idx, wlan in enumerate(self.wlans):
                fname = '/proc/net/rtl88x2eu/%s/thermal_state' % (wlan,)
                try:
                    with open(fname) as fd:
                        for line in fd:
                            line = line.strip()
                            if not line:
                                continue

                            d = {}
                            for f in line.split(','):
                                k, v = f.split(':', 1)
                                d[k.strip()] = int(v.strip())

                            ant_id = (idx << 8) + d['rf_path']
                            res[ant_id] = d['temperature']
                except FileNotFoundError:
                    pass
                except Exception as v:
                    reactor.callFromThread(log.err, v, 'Unable to parse %s:' % (fname,))
            return res

        def _got_temp(temp_d):
            self.rf_temperature = temp_d

        return threads.deferToThread(_read_temperature).addCallback(_got_temp)


    def add_ant_sel_cb(self, ant_sel_cb):
        self.ant_sel_cb_list.append(ant_sel_cb)
        ant_sel_cb(self.tx_sel)

    def add_rssi_cb(self, rssi_cb):
        self.rssi_cb_l.append(rssi_cb)

    def _stats_agg_by_freq(self, ant_stats):
        stats_agg = {}

        for (((freq, mcs_index, bandwidth), ant_id),
             (pkt_s,
              rssi_min, rssi_avg, rssi_max,
              snr_min, snr_avg, snr_max)) in ant_stats.items():

            if ant_id not in stats_agg:
                stats_agg[ant_id] = (pkt_s,
                                     rssi_min, rssi_avg * pkt_s, rssi_max,
                                     snr_min, snr_avg * pkt_s, snr_max)
            else:
                tmp = stats_agg[ant_id]
                stats_agg[ant_id] = (pkt_s + tmp[0],
                                    min(rssi_min, tmp[1]),
                                    rssi_avg * pkt_s + tmp[2],
                                    max(rssi_max, tmp[3]),
                                    min(snr_min, tmp[4]),
                                    snr_avg * pkt_s + tmp[5],
                                    max(snr_max, tmp[6]))

        return dict((ant_id, (pkt_s,
                              rssi_min, rssi_avg // pkt_s, rssi_max,
                              snr_min, snr_avg // pkt_s, snr_max)) \
                    for ant_id, (pkt_s,
                                 rssi_min, rssi_avg, rssi_max,
                                 snr_min, snr_avg, snr_max) in stats_agg.items())

    def select_tx_antenna(self, stats_agg):
        wlan_rssi_and_pkts = {}
        max_pkts = 0

        for k, grp in groupby(sorted(((ant_id >> 8) & 0xff, pkt_s, rssi_avg) \
                                     for ant_id, (pkt_s,
                                                  rssi_min, rssi_avg, rssi_max,
                                                  snr_min, snr_avg, snr_max) in stats_agg.items()),
                              lambda x: x[0]):

            grp = list(grp)
            # Use max average rssi [dBm] from all wlan's antennas
            # Use max packet counter per antenna from all wlan's antennas
            rssi = max(rssi for _, pkt_s, rssi in grp)
            pkts = max(pkt_s for _, pkt_s, rssi in grp)
            max_pkts = max(pkts, max_pkts)
            wlan_rssi_and_pkts[k] = (rssi, pkts)

        if not wlan_rssi_and_pkts:
            return

        # Select antennas with near-maximum RX packet counters only
        tx_sel_counter_thr = max_pkts - max(self.tx_sel_counter_abs_delta, max_pkts * self.tx_sel_counter_rel_delta)
        ants_with_max_pkts = set(idx for idx, (rssi, pkt_s) in wlan_rssi_and_pkts.items() if pkt_s >= tx_sel_counter_thr)

        if not ants_with_max_pkts:
            return

        new_max_rssi, new_tx_ant = max((rssi, idx) for  idx, (rssi, pkt_s) in wlan_rssi_and_pkts.items() if idx in ants_with_max_pkts)
        cur_max_rssi = wlan_rssi_and_pkts.get(self.tx_sel, (-1000, 0))[0]

        if new_tx_ant == self.tx_sel:
            return

        if self.tx_sel in ants_with_max_pkts and new_max_rssi - cur_max_rssi < self.tx_sel_rssi_delta:
            # Already selected antenna with near-maximum RX packets counter
            # and other antennas doesn't have significally large RSSI
            return

        log.msg('Switch TX antenna #%d -> #%d, RSSI %d -> %d[dB]' % (self.tx_sel, new_tx_ant, cur_max_rssi, new_max_rssi))

        for ant_sel_cb in self.ant_sel_cb_list:
            try:
                ant_sel_cb(new_tx_ant)
            except Exception:
                log.err()

        self.tx_sel = new_tx_ant

    def process_new_session(self, rx_id, session):
        if self.logger is not None:
            self.logger.send_stats(dict(type='new_session',
                                        timestamp = time.time(),
                                        id=rx_id,
                                        **session))

    def update_rx_stats(self, rx_id, packet_stats, ant_stats, session):
        stats_agg = self._stats_agg_by_freq(ant_stats)
        card_rssi_l = list(rssi_avg
                           for pkt_s,
                               rssi_min, rssi_avg, rssi_max,
                               snr_min, snr_avg, snr_max
                           in stats_agg.values())

        if stats_agg and self.ant_sel_cb_list:
            self.select_tx_antenna(stats_agg)

        if self.rssi_cb_l:
            _idx = 0 if settings.common.mavlink_err_rate else 1
            flags = 0

            if not card_rssi_l:
                flags |= WFBFlags.LINK_LOST

            elif packet_stats['dec_err'][0] + packet_stats['bad'][0] > 0:
                flags |= WFBFlags.LINK_JAMMED

            rx_errors = min(packet_stats['dec_err'][_idx] + packet_stats['bad'][_idx] + packet_stats['lost'][_idx], 65535)
            rx_fec = min(packet_stats['fec_rec'][_idx], 65535)
            mav_rssi = (max(card_rssi_l) if card_rssi_l else -128) % 256

            for rssi_cb in self.rssi_cb_l:
                try:
                    rssi_cb(rx_id, mav_rssi, rx_errors, rx_fec, flags)
                except Exception:
                    log.err()

        if settings.common.debug:
            log.msg('%s rssi %s tx#%d %s %s' % (rx_id, max(card_rssi_l) if card_rssi_l else 'N/A', self.tx_sel, packet_stats, ant_stats))

        # Send stats to CLI sessions and logger
        for s in self.ui_sessions:
            s.send_stats(dict(type='rx',
                              timestamp = time.time(),
                              id=rx_id, tx_ant=self.tx_sel,
                              packets=packet_stats, rx_ant_stats=ant_stats,
                              session=session))

    def update_tx_stats(self, tx_id, packet_stats, ant_latency):
        if settings.common.debug:
            log.msg("%s %r %r" % (tx_id, packet_stats, ant_latency))

        # Send stats to CLI sessions and logger
        for s in self.ui_sessions:
            s.send_stats(dict(type='tx',
                              timestamp = time.time(),
                              id=tx_id,
                              packets=packet_stats,
                              latency=ant_latency,
                              rf_temperature=self.rf_temperature))



class RXAntennaProtocol(LineReceiver):
    delimiter = b'\n'

    """
    wfb_rx log parser
    """

    def __init__(self, ant_stat_cb, rx_id):
        self.ant_stat_cb = ant_stat_cb
        self.rx_id = rx_id
        self.ant = {}
        self.count_all = None
        self.session = None

    def lineReceived(self, line):
        line = line.decode('utf-8').strip()
        cols = line.split('\t')

        try:
            if len(cols) < 2:
                raise BadTelemetry()

            #ts = int(cols[0])
            cmd = cols[1]

            if cmd == 'RX_ANT':
                if len(cols) != 5:
                    raise BadTelemetry()
                self.ant[(tuple(int(i) for i in cols[2].split(':')), int(cols[3], 16))] = tuple(int(i) for i in cols[4].split(':'))

            elif cmd == 'PKT':
                if len(cols) != 3:
                    raise BadTelemetry()

                p_all, b_all, p_dec_err, p_dec_ok, p_fec_rec, p_lost, p_bad, p_outgoing, b_outgoing = list(int(i) for i in cols[2].split(':'))

                if not self.count_all:
                    self.count_all = (p_all, b_all, p_dec_ok, p_fec_rec, p_lost, p_dec_err, p_bad, p_outgoing, b_outgoing)
                else:
                    self.count_all = tuple((a + b) for a, b in zip((p_all, b_all, p_dec_ok, p_fec_rec, p_lost, p_dec_err, p_bad, p_outgoing, b_outgoing),
                                                                   self.count_all))

                stats = dict(zip(('all', 'all_bytes', 'dec_ok', 'fec_rec', 'lost', 'dec_err', 'bad', 'out', 'out_bytes'),
                                 zip((p_all, b_all, p_dec_ok, p_fec_rec, p_lost, p_dec_err, p_bad, p_outgoing, b_outgoing),
                                     self.count_all)))

                # Send stats to aggregators
                if self.ant_stat_cb is not None:
                    self.ant_stat_cb.update_rx_stats(self.rx_id, stats, dict(self.ant), self.session)

                self.ant.clear()

            elif cmd == 'SESSION':
                if len(cols) != 3:
                    raise BadTelemetry()

                epoch, fec_type, fec_k, fec_n = list(int(i) for i in cols[2].split(':'))
                self.session = dict(fec_type=fec_types.get(fec_type, 'Unknown'), fec_k=fec_k, fec_n=fec_n, epoch=epoch)
                log.msg('New session detected [%s]: FEC=%s K=%d, N=%d, epoch=%d' % (self.rx_id, fec_types.get(fec_type, 'Unknown'), fec_k, fec_n, epoch))

                if self.ant_stat_cb is not None:
                    self.ant_stat_cb.process_new_session(self.rx_id, self.session)
            else:
                raise BadTelemetry()
        except BadTelemetry:
            log.msg('Bad telemetry [%s]: %s' % (self.rx_id, line), isError=1)


class DbgProtocol(LineReceiver):
    delimiter = b'\n'

    """
    stderr parser
    """

    def __init__(self, rx_id):
        self.rx_id = rx_id

    def lineReceived(self, line):
        log.msg('%s: %s' % (self.rx_id, line.decode('utf-8')))



class TXAntennaProtocol(LineReceiver):
    delimiter = b'\n'

    def __init__(self, ant_stat_cb, tx_id, ports_df, control_port_df):
        self.ant_stat_cb = ant_stat_cb
        self.tx_id = tx_id
        self.ports_df = ports_df
        self.control_port_df = control_port_df
        self.ports = {}
        self.control_port = None
        self.ant = {}
        self.count_all = None

    def lineReceived(self, line):
        cols = line.decode('utf-8').strip().split('\t')
        if len(cols) < 2:
            return

        #ts = int(cols[0])
        cmd = cols[1]

        if cmd == 'LISTEN_UDP' and len(cols) == 3:
            port, wlan = cols[2].split(':', 1)
            self.ports[wlan] = int(port)

        elif cmd == 'LISTEN_UDP_END' and self.ports_df is not None:
            self.ports_df.callback(self.ports)

        elif cmd == 'LISTEN_UDP_CONTROL' and len(cols) == 3 and self.control_port_df is not None:
            port = cols[2]
            self.control_port = int(port)
            self.control_port_df.callback(self.control_port)

        elif cmd == 'TX_ANT':
            if len(cols) != 4:
                raise BadTelemetry()
            self.ant[int(cols[2], 16)] = tuple(int(i) for i in cols[3].split(':'))

        elif cmd == 'PKT':
            if len(cols) != 3:
                raise BadTelemetry()

            p_fec_timeouts, p_incoming, b_incoming, p_injected, b_injected, p_dropped, p_truncated = list(int(i) for i in cols[2].split(':'))

            if not self.count_all:
                self.count_all = (p_fec_timeouts, p_incoming, b_incoming, p_injected, b_injected, p_dropped, p_truncated)
            else:
                self.count_all = tuple((a + b) for a, b in zip((p_fec_timeouts, p_incoming, b_incoming, p_injected, b_injected, p_dropped, p_truncated),
                                                               self.count_all))

            stats = dict(zip(('fec_timeouts', 'incoming', 'incoming_bytes', 'injected', 'injected_bytes', 'dropped', 'truncated'),
                             zip((p_fec_timeouts, p_incoming, b_incoming, p_injected, b_injected, p_dropped, p_truncated),
                                 self.count_all)))

            # Send stats to aggregators
            if self.ant_stat_cb is not None:
                self.ant_stat_cb.update_tx_stats(self.tx_id, stats, dict(self.ant))

            self.ant.clear()


class RXProtocol(ProcessProtocol):
    """
    manager for wfb_rx process
    """

    def __init__(self, ant_stat_cb, cmd, rx_id):
        self.cmd = cmd
        self.rx_id = rx_id
        self.ant = RXAntennaProtocol(ant_stat_cb, rx_id) if ant_stat_cb else None
        self.dbg = DbgProtocol(rx_id)
        self.df = defer.Deferred()

    def connectionMade(self):
        log.msg('Started %s' % (self.rx_id,))

    def outReceived(self, data):
        if self.ant is not None:
            self.ant.dataReceived(data)

    def errReceived(self, data):
        self.dbg.dataReceived(data)

    def processEnded(self, status):
        rc = status.value.exitCode
        log.msg('Stopped RX %s with code %s' % (self.rx_id, rc))

        if rc == 0:
            self.df.callback(str(status.value))
        else:
            self.df.errback(status)

    def start(self):
        df = defer.maybeDeferred(reactor.spawnProcess, self, self.cmd[0], self.cmd, env=os.environ, childFDs={0: "w", 1: "r", 2: "r"})
        return df.addCallback(lambda _: self.df)


class TXProtocol(ProcessProtocol):
    """
    manager for wfb_tx process
    """

    def __init__(self, ant_stat_cb, cmd, tx_id, ports_df=None, control_port_df=None):
        self.cmd = cmd
        self.tx_id = tx_id
        self.dbg = DbgProtocol(tx_id)
        self.ports_df = ports_df
        self.control_port_df = control_port_df
        self.port_parser = TXAntennaProtocol(ant_stat_cb, tx_id, ports_df, control_port_df)
        self.df = defer.Deferred()

    def connectionMade(self):
        log.msg('Started %s' % (self.tx_id,))

    def outReceived(self, data):
        self.port_parser.dataReceived(data)

    def errReceived(self, data):
        self.dbg.dataReceived(data)

    def processEnded(self, status):
        rc = status.value.exitCode
        log.msg('Stopped TX %s with code %s' % (self.tx_id, rc))

        if self.ports_df is not None:
            self.ports_df.cancel()

        if self.control_port_df is not None:
            self.control_port_df.cancel()

        if rc == 0:
            self.df.callback(str(status.value))
        else:
            self.df.errback(status)

    def start(self):
        df = defer.maybeDeferred(reactor.spawnProcess, self, self.cmd[0], self.cmd, env=os.environ,
                                 childFDs={0: "w", 1: "r", 2: "r"})
        return df.addCallback(lambda _: self.df)


@defer.inlineCallbacks
def init_wlans(max_bw, wlans):
    if max_bw == 10:
        ht_mode = '10MHz'
    elif max_bw == 20:
        ht_mode = 'HT20'
    elif max_bw == 40:
        ht_mode = 'HT40+'
    elif max_bw == 80:
        ht_mode = '80MHz'
    elif max_bw == 160:
        ht_mode = '160MHz'
    else:
        raise Exception('Unsupported bandwidth %d MHz' % (max_bw,))

    if not settings.common.primary:
        log.msg('Skip card init due to secondary role')
        return

    try:
        yield call_and_check_rc('iw', 'reg', 'set', settings.common.wifi_region)
        for wlan in wlans:
            if settings.common.set_nm_unmanaged and os.path.exists('/usr/bin/nmcli'):
                device_status = yield call_and_check_rc('nmcli', 'device', 'show', wlan, log_stdout=False)
                if not b'(unmanaged)' in device_status:
                    log.msg('Switch %s to unmanaged state' % (wlan,))
                    yield call_and_check_rc('nmcli', 'device', 'set', wlan, 'managed', 'no')
                    yield df_sleep(1)

            yield call_and_check_rc('ip', 'link', 'set', wlan, 'down')
            yield call_and_check_rc('iw', 'dev', wlan, 'set', 'monitor', 'otherbss')
            yield call_and_check_rc('ip', 'link', 'set', wlan, 'up')

            # You can set own frequency channel for each card
            if isinstance(settings.common.wifi_channel, dict):
                channel = settings.common.wifi_channel[wlan]
            else:
                channel = settings.common.wifi_channel

            yield call_and_check_rc('iw', 'dev', wlan, 'set', 'channel', str(channel), ht_mode)

            if settings.common.wifi_txpower:
                yield call_and_check_rc('iw', 'dev', wlan, 'set', 'txpower', 'fixed', str(settings.common.wifi_txpower))
    except ExecError as v:
        if v.stdout:
            log.msg(v.stdout, isError=1)
        if v.stderr:
            log.msg(v.stderr, isError=1)
        raise


def parse_services(profile_name):
    res = []
    for stream in getattr(settings, profile_name).streams:
        cfg = Section()
        stream = dict(stream)
        name = stream.pop('name')
        service_type = stream.pop('service_type')

        for profile in stream.pop('profiles'):
            cfg.__dict__.update(getattr(settings, profile).__dict__)

        cfg.__dict__.update(stream)
        res.append((name, service_type, cfg))

    return res


@defer.inlineCallbacks
def init(profiles, wlans):
    type_map = dict(udp_direct_rx=init_udp_direct_rx,
                    udp_direct_tx=init_udp_direct_tx,
                    mavlink=init_mavlink,
                    tunnel=init_tunnel,
                    udp_proxy=init_udp_proxy)

    services = list((profile, parse_services(profile)) for profile in profiles)
    max_bw = max(cfg.bandwidth for _, tmp in services for _, _, cfg in tmp)

    # Do cards init
    yield init_wlans(max_bw, wlans)

    dl = []
    sockets = []
    ant_sel_l = []

    def _cleanup(x):
        for s in sockets:
            s.stopListening()

        for f in ant_sel_l:
            f._cleanup()

        return x

    for profile, service_list in services:
        # Domain wide antenna selector
        profile_cfg = getattr(settings, profile)

        if settings.common.binary_log_file is not None:
            logger = BinLogger(settings.common.binary_log_file % (profile,),
                               settings.path.log_dir)
        else:
            logger = None

        ant_sel_f = StatsAndSelectorFactory(profile, wlans, profile_cfg.link_domain, logger)
        ant_sel_l.append(ant_sel_f)
        link_id = int.from_bytes(hashlib.sha1(profile_cfg.link_domain.encode('utf-8')).digest()[:3], 'big')

        if profile_cfg.stats_port:
            sockets.append(reactor.listenTCP(profile_cfg.stats_port, ant_sel_f))

        for service_name, service_type, srv_cfg in service_list:
            log.msg('Starting %s/%s@%s on %s' % (profile, service_name, profile_cfg.link_domain, ', '.join(wlans)))
            dl.append(defer.maybeDeferred(type_map[service_type], service_name, srv_cfg, wlans, link_id, ant_sel_f))

    yield defer.gatherResults(dl, consumeErrors=True).addBoth(_cleanup).addErrback(lambda f: f.trap(defer.FirstError) and f.value.subFailure)


@defer.inlineCallbacks
def init_udp_direct_tx(service_name, cfg, wlans, link_id, ant_sel_f):
    if not listen_re.match(cfg.peer):
        raise Exception('%s: unsupported peer address: %s' % (service_name, cfg.peer))

    m = listen_re.match(cfg.peer)
    listen = m.group('addr'), int(m.group('port'))
    log.msg('Listen for %s stream %d on %s:%d' % (service_name, cfg.stream_tx, listen[0], listen[1]))

    cmd = ('%(cmd)s -f %(frame_type)s -p %(stream)d -u %(port)d -K %(key)s '\
           '-B %(bw)d -G %(gi)s -S %(stbc)d -L %(ldpc)d -M %(mcs)d'\
           '%(mirror)s%(force_vht)s%(qdisc)s '\
           '-k %(fec_k)d -n %(fec_n)d -T %(fec_timeout)d -F %(fec_delay)d -i %(link_id)d -R %(rcv_buf_size)d -C %(control_port)d' % \
           dict(cmd=os.path.join(settings.path.bin_dir, 'wfb_tx'),
                frame_type=cfg.frame_type,
                stream=cfg.stream_tx,
                port=listen[1],
                control_port = cfg.control_port,
                key=os.path.join(settings.path.conf_dir, cfg.keypair),
                bw=cfg.bandwidth,
                force_vht=' -V' if cfg.force_vht else '',
                qdisc=' -Q -P %d' % (cfg.fwmark,) if cfg.use_qdisc else '',
                gi="short" if cfg.short_gi else "long",
                stbc=cfg.stbc,
                ldpc=cfg.ldpc,
                mcs=cfg.mcs_index,
                mirror=' -m' if cfg.mirror else '',
                fec_k=cfg.fec_k,
                fec_n=cfg.fec_n,
                fec_timeout=cfg.fec_timeout,
                fec_delay=cfg.fec_delay,
                link_id=link_id,
                rcv_buf_size=settings.common.tx_rcv_buf_size)
           ).split() + wlans[0:(None if cfg.mirror else 1)]

    # Direct udp doesn't support TX diversity - only first card will be used.
    # But if mirror mode is enabled it will use all cards.

    if not cfg.mirror and len(wlans) > 1:
        raise Exception("udp_direct_tx doesn't supports diversity but multiple cards selected. Use udp_proxy for such case.")

    control_port_df = defer.Deferred() if cfg.control_port == 0 else None
    df = TXProtocol(ant_sel_f, cmd, 'video tx', control_port_df=control_port_df).start()
    log.msg('%s: %s' % (service_name, ' '.join(cmd),))

    control_port = cfg.control_port

    if control_port == 0:
        control_port = yield control_port_df

    log.msg('%s use wfb_tx control_port %d' % (service_name, control_port))

    yield df


def init_udp_direct_rx(service_name, cfg, wlans, link_id, ant_sel_f):
    if not connect_re.match(cfg.peer):
        raise Exception('%s: unsupported peer address: %s' % (service_name, cfg.peer))

    m = connect_re.match(cfg.peer)
    connect = m.group('addr'), int(m.group('port'))
    log.msg('Send %s stream %d to %s:%d' % (service_name, cfg.stream_rx, connect[0], connect[1]))

    cmd = ('%(cmd)s -p %(stream)d -c %(ip_addr)s -u %(port)d -K %(key)s -i %(link_id)d' % \
           dict(cmd=os.path.join(settings.path.bin_dir, 'wfb_rx'),
                stream=cfg.stream_rx,
                ip_addr=connect[0],
                port=connect[1],
                key=os.path.join(settings.path.conf_dir, cfg.keypair),
                link_id=link_id)).split() + wlans

    df = RXProtocol(ant_sel_f, cmd, '%s rx' % (service_name,)).start()

    log.msg('%s: %s' % (service_name, ' '.join(cmd),))
    return df


@defer.inlineCallbacks
def init_mavlink(service_name, cfg, wlans, link_id, ant_sel_f):
    listen = None
    connect = None
    serial = None
    osd_peer = None

    if connect_re.match(cfg.peer):
        m = connect_re.match(cfg.peer)
        connect = m.group('addr'), int(m.group('port'))
        log.msg('Connect %s stream %d(RX), %d(TX) to %s:%d' % (service_name, cfg.stream_rx, cfg.stream_tx, connect[0], connect[1]))

    elif listen_re.match(cfg.peer):
        m = listen_re.match(cfg.peer)
        listen = m.group('addr'), int(m.group('port'))
        log.msg('Listen for %s stream %d(RX), %d(TX) on %s:%d' % (service_name, cfg.stream_rx, cfg.stream_tx, listen[0], listen[1]))

    elif serial_re.match(cfg.peer):
        m = serial_re.match(cfg.peer)
        serial = m.group('dev'), int(m.group('baud'))
        log.msg('Open serial port %s on speed %d' % (serial[0], serial[1]))

    else:
        raise Exception('Unsupported peer address: %s' % (cfg.peer,))

    if cfg.osd is not None and connect_re.match(cfg.osd):
        m = connect_re.match(cfg.osd)
        osd_peer = m.group('addr'), int(m.group('port'))
        log.msg('Mirror %s stream to OSD at %s:%d' % (service_name, osd_peer[0], osd_peer[1]))

    rx_hooks = []
    tx_hooks = []

    if cfg.call_on_arm or cfg.call_on_disarm:
        arm_proto = MavlinkARMProtocol(cfg.call_on_arm, cfg.call_on_disarm)
        rx_hooks.append(arm_proto.dataReceived)
        tx_hooks.append(arm_proto.dataReceived)

    if cfg.log_messages and ant_sel_f.logger is not None:
        mav_log_proto = MavlinkLoggerProtocol(ant_sel_f.logger)
        rx_hooks.append(mav_log_proto.dataReceived)
        tx_hooks.append(mav_log_proto.dataReceived)

    if serial:
        p_in = MavlinkSerialProxyProtocol(agg_max_size=settings.common.radio_mtu,
                                          agg_timeout=settings.common.mavlink_agg_timeout,
                                          inject_rssi=cfg.inject_rssi,
                                          mavlink_sys_id=cfg.mavlink_sys_id,
                                          mavlink_comp_id=cfg.mavlink_comp_id,
                                          rx_hooks=rx_hooks, tx_hooks=tx_hooks)
    else:
        # The first argument is not None only if we initiate mavlink connection
        p_in = MavlinkUDPProxyProtocol(connect, agg_max_size=settings.common.radio_mtu,
                                       agg_timeout=settings.common.mavlink_agg_timeout,
                                       inject_rssi=cfg.inject_rssi,
                                       mirror=osd_peer,
                                       mavlink_sys_id=cfg.mavlink_sys_id,
                                       mavlink_comp_id=cfg.mavlink_comp_id,
                                       rx_hooks=rx_hooks, tx_hooks=tx_hooks)

    p_rx = UDPProxyProtocol()
    p_rx.peer = p_in

    rx_socket = reactor.listenUDP(0, p_rx)
    sockets = [rx_socket]

    cmd_rx = ('%(cmd)s -p %(stream)d -u %(port)d -K %(key)s -i %(link_id)d' % \
              dict(cmd=os.path.join(settings.path.bin_dir, 'wfb_rx'),
                   stream=cfg.stream_rx,
                   port=rx_socket.getHost().port,
                   key=os.path.join(settings.path.conf_dir, cfg.keypair),
                   link_id=link_id)).split() + wlans

    cmd_tx = ('%(cmd)s -f %(frame_type)s -p %(stream)d -u %(port)d -K %(key)s -B %(bw)d '\
              '-G %(gi)s -S %(stbc)d -L %(ldpc)d -M %(mcs)d'\
              '%(mirror)s%(force_vht)s%(qdisc)s '\
              '-k %(fec_k)d -n %(fec_n)d -T %(fec_timeout)d -F %(fec_delay)d -i %(link_id)d -R %(rcv_buf_size)d -C %(control_port)d' % \
              dict(cmd=os.path.join(settings.path.bin_dir, 'wfb_tx'),
                   frame_type=cfg.frame_type,
                   stream=cfg.stream_tx,
                   port=0,
                   control_port=cfg.control_port,
                   key=os.path.join(settings.path.conf_dir, cfg.keypair),
                   bw=cfg.bandwidth,
                   force_vht=' -V' if cfg.force_vht else '',
                   qdisc=' -Q -P %d' % (cfg.fwmark,) if cfg.use_qdisc else '',
                   gi="short" if cfg.short_gi else "long",
                   stbc=cfg.stbc,
                   ldpc=cfg.ldpc,
                   mcs=cfg.mcs_index,
                   mirror=' -m' if cfg.mirror else '',
                   fec_k=cfg.fec_k,
                   fec_n=cfg.fec_n,
                   fec_timeout=cfg.fec_timeout,
                   fec_delay=cfg.fec_delay,
                   link_id=link_id,
                   rcv_buf_size=settings.common.tx_rcv_buf_size)).split() + wlans

    log.msg('%s RX: %s' % (service_name, ' '.join(cmd_rx)))
    log.msg('%s TX: %s' % (service_name, ' '.join(cmd_tx)))

    # Setup mavlink TCP proxy
    if cfg.mavlink_tcp_port:
        mav_tcp_f = MavlinkTCPFactory(p_in)
        p_in.rx_hooks.append(mav_tcp_f.write)
        reactor.listenTCP(cfg.mavlink_tcp_port, mav_tcp_f)

    tx_ports_df = defer.Deferred()
    control_port_df = defer.Deferred() if cfg.control_port == 0 else None

    dl = [TXProtocol(ant_sel_f, cmd_tx, '%s tx' % (service_name,), tx_ports_df, control_port_df).start()]

    # Wait while wfb_tx allocates ephemeral udp ports and reports them back
    tx_ports = yield tx_ports_df
    control_port = cfg.control_port

    if control_port == 0:
        control_port = yield control_port_df

    log.msg('%s use wfb_tx ports %s, control_port %d' % (service_name, tx_ports, control_port))

    p_tx_l = [UDPProxyProtocol(('127.0.0.1', tx_ports[wlan])) for wlan in wlans]

    if serial:
        serial_port = SerialPort(p_in, os.path.join('/dev', serial[0]), reactor, baudrate=serial[1])
        serial_port._serial.exclusive = True

    else:
        serial_port = None
        sockets += [ reactor.listenUDP(listen[1] if listen else 0, p_in) ]

    sockets += [ reactor.listenUDP(0, p_tx) for p_tx in p_tx_l ]

    def ant_sel_cb(ant_idx):
        p_in.peer = p_tx_l[ant_idx]

    ant_sel_f.add_ant_sel_cb(ant_sel_cb)

    # Report RSSI to OSD
    ant_sel_f.add_rssi_cb(p_in.send_rssi)

    dl.append(RXProtocol(ant_sel_f, cmd_rx, '%s rx' % (service_name,)).start())

    def _cleanup(x):
        if serial_port is not None:
            serial_port.loseConnection()
            serial_port.connectionLost(failure.Failure(ti_main.CONNECTION_DONE))

        for s in sockets:
            s.stopListening()

        return x

    yield defer.gatherResults(dl, consumeErrors=True).addBoth(_cleanup)\
                                                     .addErrback(lambda f: f.trap(defer.FirstError) and f.value.subFailure)


@defer.inlineCallbacks
def init_tunnel(service_name, cfg, wlans, link_id, ant_sel_f):
    p_in = TUNTAPProtocol(mtu=settings.common.radio_mtu,
                          agg_timeout=settings.common.tunnel_agg_timeout)

    p_rx = UDPProxyProtocol()
    p_rx.peer = p_in

    rx_socket = reactor.listenUDP(0, p_rx)
    sockets = [rx_socket]

    cmd_rx = ('%(cmd)s -p %(stream)d -u %(port)d -K %(key)s -i %(link_id)d' % \
              dict(cmd=os.path.join(settings.path.bin_dir, 'wfb_rx'),
                   stream=cfg.stream_rx,
                   port=rx_socket.getHost().port,
                   key=os.path.join(settings.path.conf_dir, cfg.keypair),
                   link_id=link_id)).split() + wlans

    cmd_tx = ('%(cmd)s -f %(frame_type)s -p %(stream)d -u %(port)d -K %(key)s -B %(bw)d -G %(gi)s '\
              '-S %(stbc)d -L %(ldpc)d -M %(mcs)d'\
              '%(mirror)s%(force_vht)s%(qdisc)s '\
              '-k %(fec_k)d -n %(fec_n)d -T %(fec_timeout)d -F %(fec_delay)d -i %(link_id)d -R %(rcv_buf_size)d -C %(control_port)d' % \
              dict(cmd=os.path.join(settings.path.bin_dir, 'wfb_tx'),
                   frame_type=cfg.frame_type,
                   stream=cfg.stream_tx,
                   port=0,
                   control_port=cfg.control_port,
                   key=os.path.join(settings.path.conf_dir, cfg.keypair),
                   bw=cfg.bandwidth,
                   force_vht=' -V' if cfg.force_vht else '',
                   qdisc=' -Q -P %d' % (cfg.fwmark,) if cfg.use_qdisc else '',
                   gi="short" if cfg.short_gi else "long",
                   stbc=cfg.stbc,
                   ldpc=cfg.ldpc,
                   mcs=cfg.mcs_index,
                   mirror=' -m' if cfg.mirror else '',
                   fec_k=cfg.fec_k,
                   fec_n=cfg.fec_n,
                   fec_timeout=cfg.fec_timeout,
                   fec_delay=cfg.fec_delay,
                   link_id=link_id,
                   rcv_buf_size=settings.common.tx_rcv_buf_size)).split() + wlans

    log.msg('%s RX: %s' % (service_name, ' '.join(cmd_rx)))
    log.msg('%s TX: %s' % (service_name, ' '.join(cmd_tx),))

    tx_ports_df = defer.Deferred()
    control_port_df = defer.Deferred() if cfg.control_port == 0 else None

    dl = [TXProtocol(ant_sel_f, cmd_tx, '%s tx' % (service_name,), tx_ports_df, control_port_df).start()]

    # Wait while wfb_tx allocates ephemeral udp ports and reports them back
    tx_ports = yield tx_ports_df
    control_port = cfg.control_port

    if control_port == 0:
        control_port = yield control_port_df

    log.msg('%s use wfb_tx ports %s, control_port %d' % (service_name, tx_ports, control_port))

    p_tx_l = [UDPProxyProtocol(('127.0.0.1', tx_ports[wlan])) for wlan in wlans]

    tun_ep = TUNTAPTransport(reactor, p_in, cfg.ifname, cfg.ifaddr, mtu=settings.common.radio_mtu, default_route=cfg.default_route)

    sockets += [ reactor.listenUDP(0, p_tx) for p_tx in p_tx_l ]

    def ant_sel_cb(ant_idx):
        p_in.peer = p_tx_l[ant_idx]

    # Broadcast keepalive message to all cards, not to active one
    # This allow to use direct antennas on both ends and/or differenct frequencies.
    # But when mirroring enabled it will be done by wfb_tx itself

    if cfg.mirror:
        p_in.all_peers = [p_tx_l[0]]
    else:
        p_in.all_peers = p_tx_l

    ant_sel_f.add_ant_sel_cb(ant_sel_cb)

    dl.append(RXProtocol(ant_sel_f, cmd_rx, '%s rx' % (service_name,)).start())

    def _cleanup(x):
        tun_ep.loseConnection()
        for s in sockets:
            s.stopListening()
        return x

    yield defer.gatherResults(dl, consumeErrors=True).addBoth(_cleanup)\
                                                     .addErrback(lambda f: f.trap(defer.FirstError) and f.value.subFailure)


@defer.inlineCallbacks
def init_udp_proxy(service_name, cfg, wlans, link_id, ant_sel_f):
    listen = None
    connect = None

    if connect_re.match(cfg.peer):
        m = connect_re.match(cfg.peer)
        connect = m.group('addr'), int(m.group('port'))
        log.msg('Connect %s stream %s(RX), %s(TX) to %s:%d' % (service_name, cfg.stream_rx, cfg.stream_tx, connect[0], connect[1]))

    elif listen_re.match(cfg.peer):
        m = listen_re.match(cfg.peer)
        listen = m.group('addr'), int(m.group('port'))
        log.msg('Listen for %s stream %s(RX), %s(TX) on %s:%d' % (service_name, cfg.stream_rx, cfg.stream_tx, listen[0], listen[1]))

    else:
        raise Exception('Unsupported peer address: %s' % (cfg.peer,))

    # The first argument is not None only if we initiate mavlink connection
    p_in = UDPProxyProtocol(connect)
    sockets = [reactor.listenUDP(listen[1] if listen else 0, p_in)]
    dl = []

    if cfg.stream_rx is not None:
        p_rx = UDPProxyProtocol()
        p_rx.peer = p_in
        rx_socket = reactor.listenUDP(0, p_rx)
        sockets = [rx_socket]
        cmd_rx = ('%(cmd)s -p %(stream)d -u %(port)d -K %(key)s -i %(link_id)d' % \
                  dict(cmd=os.path.join(settings.path.bin_dir, 'wfb_rx'),
                       stream=cfg.stream_rx,
                       port=rx_socket.getHost().port,
                       key=os.path.join(settings.path.conf_dir, cfg.keypair),
                       link_id=link_id)).split() + wlans
        log.msg('%s RX: %s' % (service_name, ' '.join(cmd_rx)))
        dl.append(RXProtocol(ant_sel_f, cmd_rx, '%s rx' % (service_name,)).start())

    if cfg.stream_tx is not None:
        cmd_tx = ('%(cmd)s -f %(frame_type)s -p %(stream)d -u %(port)d -K %(key)s -B %(bw)d '\
                  '-G %(gi)s -S %(stbc)d -L %(ldpc)d -M %(mcs)d'\
                  '%(mirror)s%(force_vht)s%(qdisc)s '\
                  '-k %(fec_k)d -n %(fec_n)d -T %(fec_timeout)d -F %(fec_delay)d -i %(link_id)d -R %(rcv_buf_size)d -C %(control_port)d' % \
                  dict(cmd=os.path.join(settings.path.bin_dir, 'wfb_tx'),
                       frame_type=cfg.frame_type,
                       stream=cfg.stream_tx,
                       port=0,
                       control_port=cfg.control_port,
                       key=os.path.join(settings.path.conf_dir, cfg.keypair),
                       bw=cfg.bandwidth,
                       force_vht=' -V' if cfg.force_vht else '',
                       qdisc=' -Q -P %d' % (cfg.fwmark,) if cfg.use_qdisc else '',
                       gi="short" if cfg.short_gi else "long",
                       stbc=cfg.stbc,
                       ldpc=cfg.ldpc,
                       mcs=cfg.mcs_index,
                       mirror=' -m' if cfg.mirror else '',
                       fec_k=cfg.fec_k,
                       fec_n=cfg.fec_n,
                       fec_timeout=cfg.fec_timeout,
                       fec_delay=cfg.fec_delay,
                       link_id=link_id,
                       rcv_buf_size=settings.common.tx_rcv_buf_size)).split() + wlans
        log.msg('%s TX: %s' % (service_name, ' '.join(cmd_tx)))

        tx_ports_df = defer.Deferred()
        control_port_df = defer.Deferred() if cfg.control_port == 0 else None

        dl += [TXProtocol(ant_sel_f, cmd_tx, '%s tx' % (service_name,), tx_ports_df, control_port_df).start()]

        # Wait while wfb_tx allocates ephemeral udp ports and reports them back
        tx_ports = yield tx_ports_df
        control_port = cfg.control_port

        if control_port == 0:
            control_port = yield control_port_df

        log.msg('%s use wfb_tx ports %s, control_port %d' % (service_name, tx_ports, control_port))

        p_tx_l = [UDPProxyProtocol(('127.0.0.1', tx_ports[wlan])) for wlan in wlans]
        sockets += [reactor.listenUDP(0, p_tx) for p_tx in p_tx_l ]

        def ant_sel_cb(ant_idx):
            p_in.peer = p_tx_l[ant_idx]

        ant_sel_f.add_ant_sel_cb(ant_sel_cb)

    def _cleanup(x):
        for s in sockets:
            s.stopListening()

        return x

    yield defer.gatherResults(dl, consumeErrors=True).addBoth(_cleanup)\
                                                     .addErrback(lambda f: f.trap(defer.FirstError) and f.value.subFailure)

def main():
    log.msg = _log_msg

    if settings.common.log_file:
        log.startLogging(ErrorSafeLogFile(settings.common.log_file,
                                          settings.path.log_dir,
                                          rotateLength=1024 * 1024,
                                          maxRotatedFiles=10))

    elif sys.stdout.isatty():
        log.startLogging(sys.stdout)

    else:
        obs = ConsoleObserver()
        log.theLogPublisher._startLogging(obs.emit, False)


    log.msg('WFB-ng version %s-%s' % (settings.common.version, settings.common.commit[:8]))
    profiles, wlans = sys.argv[1], list(wlan for arg in sys.argv[2:] for wlan in arg.split())
    uname = os.uname()
    log.msg('Run on %s/%s @%s, profile(s) %s using %s' % (uname[4], uname[2], uname[1], profiles, ', '.join(wlans)))
    log.msg('Using cfg files:\n%s' % ('\n'.join(cfg_files),))

    reactor.callWhenRunning(lambda: defer.maybeDeferred(init, profiles.split(':'), wlans)\
                            .addErrback(abort_on_crash))
    reactor.run()

    rc = exit_status()
    log.msg('Exiting with code %d' % rc)
    sys.exit(rc)


if __name__ == '__main__':
    main()
