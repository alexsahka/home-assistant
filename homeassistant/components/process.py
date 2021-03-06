#!/usr/bin/env python
# -*- coding: utf-8 -*-
# -*- Python -*-
#
# $Id: process.py $
#
# Author: Markus Stenberg <fingon@iki.fi>
#
# Copyright (c) 2014 Markus Stenberg
#
# Created:       Wed Apr 23 23:33:26 2014 mstenber
# Last modified: Thu Apr 24 17:13:04 2014 mstenber
# Edit time:     19 min
#
"""
homeassistant.components.process
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Provides functionality to watch for specific processes running
on the host machine.
"""

import os

from homeassistant.components import STATE_ON, STATE_OFF
import homeassistant.util as util

DOMAIN = 'process'
ENTITY_ID_FORMAT = DOMAIN + '.{}'

PS_STRING = 'ps awx'


def setup(hass, processes):
    """ Sets up a check if specified processes are running.

        processes: dict mapping entity id to substring to search for
                   in process list.
    """

    entities = {ENTITY_ID_FORMAT.format(util.slugify(pname)): pstring
                for pname, pstring in processes.items()}

    # pylint: disable=unused-argument
    def update_process_states(time):
        """ Check ps for currently running processes and update states. """
        with os.popen(PS_STRING, 'r') as psfile:
            lines = list(psfile)

        for entity_id, pstring in entities.items():
            state = STATE_ON if any(pstring in l for l in lines) else STATE_OFF

            hass.states.set(entity_id, state)

    update_process_states(None)

    hass.track_time_change(update_process_states, second=[0, 30])

    return True
