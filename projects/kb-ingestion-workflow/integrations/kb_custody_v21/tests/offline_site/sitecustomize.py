"""Propagate the offline socket guard into Python conversion subprocesses."""
import os
import sys

if os.environ.get('KB_V21_OFFLINE_TEST') == '1':
    def deny_network(event, args):
        if event in ('socket.connect', 'socket.connect_ex', 'socket.bind', 'socket.getaddrinfo'):
            raise RuntimeError('network disabled in V2.1 offline child process')
    sys.addaudithook(deny_network)
