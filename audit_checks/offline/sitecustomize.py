"""Test process network deny; no production transport or credential is provided."""
import socket


def deny(*args, **kwargs):
    raise AssertionError('Audit regressions must use synthetic transports; network denied')


socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
