# -*- coding: utf-8 -*-

#    Copyright (C) 2014 AT&T Labs All Rights Reserved.
#    Copyright (C) 2014 University of Pennsylvania All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging

from kazoo import client
from oslo.config import cfg

from ryu import log

LOGGER = logging.getLogger('cleanup-zk')

CONF = cfg.CONF
CONF.import_opt('zk_servers', 'ryu.app.inception_conf')
CONF.import_opt('zk_data', 'ryu.app.inception_conf')
CONF.import_opt('zk_failover', 'ryu.app.inception_conf')
CONF.import_opt('zk_election', 'ryu.app.inception_conf')
CONF.import_opt('zk_log_level', 'ryu.app.inception_conf')


def main():
    try:
        CONF(project='ryu',
             default_config_files=['/usr/local/etc/ryu/ryu.conf'])
    except cfg.ConfigFilesNotFoundError:
        CONF(project='ryu')

    log.init_log()

    zk_logger = logging.getLogger('kazoo')
    zk_log_level = log.LOG_LEVELS[CONF.zk_log_level]
    zk_logger.setLevel(zk_log_level)
    zk_console_handler = logging.StreamHandler()
    zk_console_handler.setLevel(zk_log_level)
    zk_console_handler.setFormatter(logging.Formatter(CONF.log_formatter))
    zk_logger.addHandler(zk_console_handler)
    zk = client.KazooClient(hosts=CONF.zk_servers, logger=zk_logger)
    zk.start()

    LOGGER.info('Clean up ZooKeeper path=(%s)', CONF.zk_data)
    zk.delete(CONF.zk_data, recursive=True)
    LOGGER.info('Clean up ZooKeeper path=(%s)', CONF.zk_failover)
    zk.delete(CONF.zk_failover, recursive=True)
    LOGGER.info('Clean up ZooKeeper path=(%s)', CONF.zk_election)
    zk.delete(CONF.zk_election, recursive=True)


if __name__ == "__main__":
    main()
