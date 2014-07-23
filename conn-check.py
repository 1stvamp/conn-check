#!/usr/bin/python -uWignore
"""Check connectivity to various services."""

# We need the relative import here, we need it to fix the path
import _pythonpath

import os
import re
import sys
import time
import glob
import errno
import urllib
import traceback

import redis
import psycopg2

from optparse import OptionParser
from urlparse import urlsplit
from itertools import izip
from threading import Thread

import gdata.contacts.client

from OpenSSL import SSL
from OpenSSL.crypto import load_certificate, FILETYPE_PEM

from twisted.internet import epollreactor
epollreactor.install()

from twisted.internet import reactor, ssl
from twisted.internet.defer import (
    returnValue,
    inlineCallbacks,
    maybeDeferred,
    DeferredList,
    Deferred)
from twisted.internet.error import DNSLookupError, TimeoutError
from twisted.internet.abstract import isIPAddress
from twisted.internet.protocol import (
    DatagramProtocol,
    Protocol,
    ClientCreator)
from twisted.python.failure import Failure
from twisted.python.threadpool import ThreadPool

from s3lib import s3lib, swiftlib
from u1config import config
from ubuntuone.amqp import AMQPClientService
from ubuntuone.storage.s3utils import get_s3_config, get_swift_config

from u1backends.account.upayclient import get_config as get_upay_config
from u1backends.account.upayclient import UbuntuPayClient
from u1backends.auth.sso import get_api_service_root
from u1backends.db.config import get_connection_settings


CONNECT_TIMEOUT = 10
BOGUS_PORT = -1
CA_CERTS = []

for certFileName in glob.glob("/etc/ssl/certs/*.pem"):
    # There might be some dead symlinks in there, so let's make sure it's real.
    if os.path.exists(certFileName):
        data = open(certFileName).read()
        x509 = load_certificate(FILETYPE_PEM, data)
        # Now, de-duplicate in case the same cert has multiple names.
        CA_CERTS.append(x509)


class VerifyingContextFactory(ssl.CertificateOptions):

    def __init__(self, verify, caCerts, verifyCallback=None):
        ssl.CertificateOptions.__init__(self, verify=verify,
                                        caCerts=caCerts,
                                        method=SSL.SSLv23_METHOD)
        self.verifyCallback = verifyCallback

    def _makeContext(self):
        context = ssl.CertificateOptions._makeContext(self)
        if self.verifyCallback is not None:
            context.set_verify(
                SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT,
                self.verifyCallback)
        return context


def maybeDeferToThread(f, *args, **kwargs):
    """
    Call the function C{f} using a thread from the given threadpool and return
    the result as a Deferred.

    @param f: The function to call. May return a deferred.
    @param *args: positional arguments to pass to f.
    @param **kwargs: keyword arguments to pass to f.

    @return: A Deferred which fires a callback with the result of f, or an
        errback with a L{twisted.python.failure.Failure} if f throws an
        exception.
    """
    threadpool = reactor.getThreadPool()

    d = Deferred()

    def realOnResult(result):
        if not isinstance(result, Failure):
            reactor.callFromThread(d.callback, result)
        else:
            reactor.callFromThread(d.errback, result)

    def onResult(success, result):
        assert success
        assert isinstance(result, Deferred)
        result.addBoth(realOnResult)

    threadpool.callInThreadWithCallback(onResult, maybeDeferred,
                                        f, *args, **kwargs)

    return d


class Check(object):
    """Abstract base class for objects embodying connectivity checks."""

    def check(self, pattern, results):
        """Run this check, if it matches the pattern.

        If the pattern matches, and this is a leaf node in the check tree,
        implementations of Check.check should call
        results.notify_start, then either results.notify_success or
        results.notify_failure.
        """
        raise NotImplementedError("%r.check not implemented" % type(self))

    def skip(self, pattern, results):
        """Indicate that this check has been skipped.

        If the pattern matches and this is a leaf node in the check tree,
        implementations of Check.skip should call results.notify_skip.
        """
        raise NotImplementedError("%r.skip not implemented" % type(self))


class Pattern(object):
    """Abstract base class for patterns used to select subsets of checks."""

    def assume_prefix(self, prefix):
        """Return an equivalent pattern with the given prefix baked in.

        For example, if self.matches("bar") is True, then
        self.assume_prefix("foo").matches("foobar") will be True.
        """
        return PrefixPattern(prefix, self)

    def failed(self):
        """Return True if the pattern cannot match any string.

        This is mainly used so we can bail out early when recursing into
        check trees.
        """
        return not self.prefix_matches("")

    def prefix_matches(self, partial_name):
        """Return True if the partial name (a prefix) is a potential match."""
        raise NotImplementedError("%r.prefix_matches not implemented" %
                                  type(self))

    def matches(self, name):
        """Return True if the given name matches."""
        raise NotImplementedError("%r.match not implemented" %
                                  type(self))


class ResultTracker(object):
    """Base class for objects which report or record check results."""

    def notify_start(self, name, info):
        """Register the start of a check."""

    def notify_skip(self, name):
        """Register a check being skipped."""

    def notify_success(self, name, duration):
        """Register a successful check."""

    def notify_failure(self, name, info, exc_info, duration):
        """Register the failure of a check."""


class PrefixResultWrapper(ResultTracker):
    """ResultWrapper wrapper which adds a prefix to recorded results."""

    def __init__(self, wrapped, prefix):
        """Initialize an instance."""
        super(PrefixResultWrapper, self).__init__()
        self.__wrapped = wrapped
        self.__prefix = prefix

    def __make_name(self, name):
        """Make a name by prepending the prefix."""
        return "%s%s" % (self.__prefix, name)

    def notify_skip(self, name):
        """Register a check being skipped."""
        self.__wrapped.notify_skip(self.__make_name(name))

    def notify_start(self, name, info):
        """Register the start of a check."""
        self.__wrapped.notify_start(self.__make_name(name), info)

    def notify_success(self, name, duration):
        """Register success."""
        self.__wrapped.notify_success(self.__make_name(name), duration)

    def notify_failure(self, name, info, exc_info, duration):
        """Register failure."""
        self.__wrapped.notify_failure(self.__make_name(name),
                                      info, exc_info, duration)


class FailureCountingResultWrapper(ResultTracker):
    """ResultWrapper wrapper which counts failures."""

    def __init__(self, wrapped):
        """Initialize an instance."""
        super(FailureCountingResultWrapper, self).__init__()
        self.__wrapped = wrapped
        self.__failure_count = 0

    def notify_skip(self, name):
        """Register a check being skipped."""
        self.__wrapped.notify_skip(name)

    def notify_start(self, name, info):
        """Register the start of a check."""
        self.__failure_count += 1
        self.__wrapped.notify_start(name, info)

    def notify_success(self, name, duration):
        """Register success."""
        self.__failure_count -= 1
        self.__wrapped.notify_success(name, duration)

    def notify_failure(self, name, info, exc_info, duration):
        """Register failure."""
        self.__wrapped.notify_failure(name, info, exc_info, duration)

    def any_failed(self):
        """Return True if any checks using this wrapper failed so far."""
        return self.__failure_count > 0


class FailedPattern(Pattern):
    """Patterns that always fail to match."""

    def assume_prefix(self, prefix):
        """Return an equivalent pattern with the given prefix baked in."""
        return FAILED_PATTERN

    def prefix_matches(self, partial_name):
        """Return True if the partial name matches."""
        return False

    def matches(self, name):
        """Return True if the complete name matches."""
        return False


FAILED_PATTERN = FailedPattern()


PATTERN_TOKEN_RE = re.compile(r'\*|[^*]+')


def tokens_to_partial_re(tokens):
    """Convert tokens to a regular expression for matching prefixes."""

    def token_to_re(token):
        """Convert tokens to (begin, end, alt_end) triples."""
        if token == '*':
            return (r'(?:.*', ')?', ')')
        else:
            chars = list(token)
            begin = "".join(["(?:" + re.escape(c) for c in chars])
            end = "".join([")?" for c in chars])
            return (begin, end, end)

    subexprs = map(token_to_re, tokens)
    if len(subexprs) > 0:
        # subexpressions like (.*)? aren't accepted, so we may have to use
        # an alternate closing form for the last (innermost) subexpression
        (begin, _, alt_end) = subexprs[-1]
        subexprs[-1] = (begin, alt_end, alt_end)
    return re.compile("".join([se[0] for se in subexprs] +
                              [se[1] for se in reversed(subexprs)] +
                              [r'\Z']))


def tokens_to_re(tokens):
    """Convert tokens to a regular expression for exact matching."""

    def token_to_re(token):
        """Convert tokens to simple regular expressions."""
        if token == '*':
            return r'.*'
        else:
            return re.escape(token)

    return re.compile("".join(map(token_to_re, tokens) + [r'\Z']))


class SimplePattern(Pattern):
    """Pattern that matches according to the given pattern expression."""

    def __init__(self, pattern):
        """Initialize an instance."""
        super(SimplePattern, self).__init__()
        tokens = PATTERN_TOKEN_RE.findall(pattern)
        self.__partial_re = tokens_to_partial_re(tokens)
        self.__full_re = tokens_to_re(tokens)

    def prefix_matches(self, partial_name):
        """Return True if the partial name matches."""
        return self.__partial_re.match(partial_name) is not None

    def matches(self, name):
        """Return True if the complete name matches."""
        return self.__full_re.match(name) is not None


class PrefixPattern(Pattern):
    """Pattern that assumes a previously given prefix."""

    def __init__(self, prefix, pattern):
        """Initialize an instance."""
        super(PrefixPattern, self).__init__()
        self.__prefix = prefix
        self.__pattern = pattern

    def assume_prefix(self, prefix):
        """Return an equivalent pattern with the given prefix baked in."""
        return PrefixPattern(self.__prefix + prefix, self.__pattern)

    def prefix_matches(self, partial_name):
        """Return True if the partial name matches."""
        return self.__pattern.prefix_matches(self.__prefix + partial_name)

    def matches(self, name):
        """Return True if the complete name matches."""
        return self.__pattern.matches(self.__prefix + name)


class SumPattern(Pattern):
    """Pattern that matches if at least one given pattern matches."""

    def __init__(self, patterns):
        """Initialize an instance."""
        super(SumPattern, self).__init__()
        self.__patterns = patterns

    def prefix_matches(self, partial_name):
        """Return True if the partial name matches."""
        for pattern in self.__patterns:
            if pattern.prefix_matches(partial_name):
                return True
        return False

    def matches(self, name):
        """Return True if the complete name matches."""
        for pattern in self.__patterns:
            if pattern.matches(name):
                return True
        return False


class ConditionalCheck(Check):
    """A Check that skips unless the given predicate is true at check time."""

    def __init__(self, wrapped, predicate):
        """Initialize an instance."""
        super(ConditionalCheck, self).__init__()
        self.__wrapped = wrapped
        self.__predicate = predicate

    def check(self, pattern, result):
        """Skip the check."""
        if self.__predicate():
            return self.__wrapped.check(pattern, result)
        else:
            self.skip(pattern, result)

    def skip(self, pattern, result):
        """Skip the check."""
        self.__wrapped.skip(pattern, result)


class FunctionCheck(Check):
    """A Check which takes a check function."""

    def __init__(self, name, check, info=None, blocking=False):
        """Initialize an instance."""
        super(FunctionCheck, self).__init__()
        self.__name = name
        self.__info = info
        self.__check = check
        self.__blocking = blocking

    @inlineCallbacks
    def check(self, pattern, results):
        """Call the check function."""
        if not pattern.matches(self.__name):
            returnValue(None)
        results.notify_start(self.__name, self.__info)
        start = time.time()
        try:
            if self.__blocking:
                result = yield maybeDeferToThread(self.__check)
            else:
                result = yield maybeDeferred(self.__check)
            results.notify_success(self.__name, time.time() - start)
            returnValue(result)
        except Exception:
            results.notify_failure(self.__name, self.__info,
                                   sys.exc_info(), time.time() - start)

    def skip(self, pattern, results):
        """Record the skip."""
        if not pattern.matches(self.__name):
            return
        results.notify_skip(self.__name)


class MultiCheck(Check):
    """A composite check comprised of multiple subchecks."""

    def __init__(self, subchecks, strategy):
        """Initialize an instance."""
        super(MultiCheck, self).__init__()
        self.__subchecks = list(subchecks)
        self.__strategy = strategy

    def check(self, pattern, results):
        """Run subchecks using the strategy supplied at creation time."""
        return self.__strategy(self.__subchecks, pattern, results)

    def skip(self, pattern, results):
        """Skip subchecks."""
        for subcheck in self.__subchecks:
            subcheck.skip(pattern, results)


class PrefixCheckWrapper(Check):
    """Runs a given check, adding a prefix to its name.

    This works by wrapping the pattern and result tracker objects
    passed to .check and .skip.
    """

    def __init__(self, wrapped, prefix):
        """Initialize an instance."""
        super(PrefixCheckWrapper, self).__init__()
        self.__wrapped = wrapped
        self.__prefix = prefix

    def __do_subcheck(self, subcheck, pattern, results):
        """Do a subcheck if the pattern could still match."""
        pattern = pattern.assume_prefix(self.__prefix)
        if not pattern.failed():
            results = PrefixResultWrapper(wrapped=results,
                                          prefix=self.__prefix)
            return subcheck(pattern, results)

    def check(self, pattern, results):
        """Run the check, prefixing results."""
        return self.__do_subcheck(self.__wrapped.check, pattern, results)

    def skip(self, pattern, results):
        """Skip checks, prefixing results."""
        self.__do_subcheck(self.__wrapped.skip, pattern, results)


@inlineCallbacks
def sequential_strategy(subchecks, pattern, results):
    """Run subchecks sequentially, skipping checks after the first failure.

    This is most useful when the failure of one check in the sequence
    would imply the failure of later checks -- for example, it probably
    doesn't make sense to run an SSL check if the basic TCP check failed.

    Use sequential_check to create a meta-check using this strategy.
    """
    local_results = FailureCountingResultWrapper(wrapped=results)
    failed = False
    for subcheck in subchecks:
        if failed:
            subcheck.skip(pattern, local_results)
        else:
            yield maybeDeferred(subcheck.check, pattern, local_results)
            if local_results.any_failed():
                failed = True


def parallel_strategy(subchecks, pattern, results):
    """A strategy which runs the given subchecks in parallel.

    Most checks can potentially block for long periods, and shouldn't have
    interdependencies, so it makes sense to run them in parallel to
    shorten the overall run time.

    Use parallel_check to create a meta-check using this strategy.
    """
    deferreds = [maybeDeferred(subcheck.check, pattern, results)
                 for subcheck in subchecks]
    return DeferredList(deferreds)


def parallel_check(subchecks):
    """Return a check that runs the given subchecks in parallel."""
    return MultiCheck(subchecks=subchecks, strategy=parallel_strategy)


def sequential_check(subchecks):
    """Return a check that runs the given subchecks in sequence."""
    return MultiCheck(subchecks=subchecks, strategy=sequential_strategy)


def add_check_prefix(*args):
    """Return an equivalent check with the given prefix prepended to its name.

    The final argument should be a check; the remaining arguments are treated
    as name components and joined with the check name using periods as
    separators.  For example, if the name of a check is "baz", then:

        add_check_prefix("foo", "bar", check)

    ...will return a check with the effective name "foo.bar.baz".
    """
    args = list(args)
    check = args.pop(-1)
    path = ".".join(args)
    return PrefixCheckWrapper(wrapped=check, prefix="%s." % (path,))


def make_check(name, check, info=None, blocking=False):
    """Make a check object from a function."""
    return FunctionCheck(name=name, check=check, info=info, blocking=blocking)


def guard_check(check, predicate):
    """Wrap a check so that it is skipped unless the predicate is true."""
    return ConditionalCheck(wrapped=check, predicate=predicate)


class TCPCheckProtocol(Protocol):

    def connectionMade(self):
        self.transport.loseConnection()


@inlineCallbacks
def do_tcp_check(host, port, ssl=False, ssl_verify=True):
    """Generic connection check function."""
    if not isIPAddress(host):
        try:
            ip = yield reactor.resolve(host, timeout=(1, CONNECT_TIMEOUT))
        except DNSLookupError:
            raise ValueError("dns resolution failed")
    else:
        ip = host
    creator = ClientCreator(reactor, TCPCheckProtocol)
    try:
        if ssl:
            context = VerifyingContextFactory(ssl_verify, CA_CERTS)
            yield creator.connectSSL(ip, port, context,
                                     timeout=CONNECT_TIMEOUT)
        else:
            yield creator.connectTCP(ip, port, timeout=CONNECT_TIMEOUT)
    except TimeoutError:
        if ip == host:
            raise ValueError("timed out")
        else:
            raise ValueError("timed out connecting to %s" % ip)


def make_tcp_check(host, port):
    """Return a check for TCP connectivity."""
    return make_check("tcp", lambda: do_tcp_check(host, port),
                      info="%s:%s" % (host, port))


def make_ssl_check(host, port, verify=True):
    """Return a check for SSL setup."""
    return make_check("ssl",
                      lambda: do_tcp_check(host, port, ssl=True),
                      info="%s:%s" % (host, port))


class UDPCheckProtocol(DatagramProtocol):

    def __init__(self, host, port, send, expect, deferred=None):
        self.host = host
        self.port = port
        self.send = send
        self.expect = expect
        self.deferred = deferred

    def _finish(self, success, result):
        if not (self.delayed.cancelled or self.delayed.called):
            self.delayed.cancel()
        if self.deferred is not None:
            if success:
                self.deferred.callback(result)
            else:
                self.deferred.errback(result)
            self.deferred = None

    def startProtocol(self):
        self.transport.write(self.send, (self.host, self.port))
        self.delayed = reactor.callLater(CONNECT_TIMEOUT,
                                         self._finish,
                                         False, TimeoutError())

    def datagramReceived(self, datagram, addr):
        if datagram == self.expect:
            self._finish(True, True)
        else:
            self._finish(False, ValueError("unexpected reply"))


@inlineCallbacks
def do_udp_check(host, port, send, expect):
    """Generic connection check function."""
    if not isIPAddress(host):
        try:
            ip = yield reactor.resolve(host, timeout=(1, CONNECT_TIMEOUT))
        except DNSLookupError:
            raise ValueError("dns resolution failed")
    else:
        ip = host
    deferred = Deferred()
    protocol = UDPCheckProtocol(host, port, send, expect, deferred)
    reactor.listenUDP(0, protocol)
    try:
        yield deferred
    except TimeoutError:
        if ip == host:
            raise ValueError("timed out")
        else:
            raise ValueError("timed out waiting for %s" % ip)


def make_udp_check(host, port, send, expect):
    """Return a check for TCP connectivity."""
    return make_check("ping", lambda: do_udp_check(host, port, send, expect),
                      info="%s:%s" % (host, port))


def make_amqp_check(host, port, use_ssl, username, password, vhost="/"):
    """Return a check for AMQP connectivity."""
    subchecks = []
    subchecks.append(make_tcp_check(host, port))

    if use_ssl:
        subchecks.append(make_ssl_check(host, port, verify=False))

    @inlineCallbacks
    def do_auth():
        """Connect and authenticate."""
        credentials = {'LOGIN': username, 'PASSWORD': password}
        service = AMQPClientService(host=host, port=port, use_ssl=use_ssl,
                                    credentials=credentials, vhost=vhost)
        yield service.startService()
        try:
            yield service.await_connection(timeout=CONNECT_TIMEOUT)
        finally:
            yield service.stopService()

    subchecks.append(make_check("auth", do_auth,
                                info="user %s" % (username,),))
    return sequential_check(subchecks)


def make_postgres_check(host, port, username, password, database):
    """Return a check for Postgres connectivity."""
    subchecks = []
    connect_kw = {'host': host, 'user': username, 'database': database}

    if host[0] != '/':
        connect_kw['port'] = port
        subchecks.append(make_tcp_check(host, port))

    if password is not None:
        connect_kw['password'] = password

    def check_auth():
        """Try to establish a postgres connection and log in."""
        conn = psycopg2.connect(**connect_kw)
        conn.close()

    subchecks.append(make_check("auth", check_auth,
                                info="user %s" % (username,),
                                blocking=True))
    return sequential_check(subchecks)


def make_rabbitmq_check(section="amqp", prefix="rabbitmq"):
    """Make a check for the configured RabbitMQ."""
    config_section = getattr(config, section)
    host = config_section.host
    port = config_section.port or BOGUS_PORT
    use_ssl = config_section.use_ssl
    username = config_section.username
    password = config_section.password
    check = make_amqp_check(host, port, use_ssl, username, password)
    return add_check_prefix(prefix, check)


def make_oops_rabbitmq_check():
    """Make a check for the configured RabbitMQ."""
    config_section = getattr(config, "oops")
    host = config_section.amqp_host
    port = config_section.amqp_port or BOGUS_PORT
    vhost = config_section.amqp_vhost
    use_ssl = config_section.amqp_ssl
    username = config_section.amqp_user
    password = config.secret.oops_amqp_password
    check = make_amqp_check(host, port, use_ssl, username, password, vhost)
    return add_check_prefix("oops.rabbitmq", check)


def make_statsd_check():
    """Make a check for the configured txstatsd."""
    servers = [(host, int(port)) for host, port in (server.split(':')
               for server in config.statsd.servers.split(';'))]

    subchecks = []
    for index, (host, port) in enumerate(servers):
        subcheck = add_check_prefix(
            "%d.statsd" % index,
            make_udp_check(host, port, "Hakuna", "Matata"))
        subchecks.append(subcheck)

    return add_check_prefix("statsd", parallel_check(subchecks))


def make_db_check(store_name, settings):
    """Make a check for a database (identified by store name)."""
    host = settings['host'] or urllib.quote(settings['db_dir'])
    check = make_postgres_check(host=host, port=settings['port'],
                                username=settings['username'],
                                password=settings['password'] or None,
                                database=settings['database'])
    return add_check_prefix("db", store_name, check)


def make_s3_check():
    """Make a check for our configured S3 service."""
    s3_config = get_s3_config()

    host = s3_config.host
    port = s3_config.port or BOGUS_PORT
    proxy_host = s3_config.proxy_host
    proxy_port = s3_config.proxy_port
    use_ssl = s3_config.use_ssl if proxy_host is None else False

    subchecks = []
    subchecks.append(make_tcp_check(
        proxy_host is not None and proxy_host or host,
        proxy_port is not None and proxy_port or port))

    if use_ssl:
        subchecks.append(make_ssl_check(
            proxy_host is not None and proxy_host or host,
            proxy_port is not None and proxy_port or port))

    storage = s3lib.S3(host=host,
                       port=port,
                       access_key=s3_config.access_key,
                       secret_key=s3_config.secret_key,
                       use_ssl=use_ssl,
                       proxy_host=proxy_host,
                       proxy_port=proxy_port,
                       connect_timeout=CONNECT_TIMEOUT,
                       timeout=CONNECT_TIMEOUT)

    @inlineCallbacks
    def check_bucket(bucket):
        """Try to connect to AWS and authenticate."""
        f = storage.stat_bucket(bucket)
        yield f.deferred

    auth_checks = []
    for name, bucket in (("bucket", s3_config.bucket),
                         ("fallback_bucket", s3_config.fallback_bucket)):
        if bucket:
            auth_checks.append(
                make_check("auth.%s" % name,
                           lambda bname=bucket: check_bucket(bname)))
    if auth_checks:
        subchecks.append(parallel_check(auth_checks))
    return add_check_prefix("s3", sequential_check(subchecks))


def make_swift_check():
    """Make a check for our configured Swift service."""
    swift_config = get_swift_config()

    host = swift_config.auth_host
    port = swift_config.auth_port or BOGUS_PORT
    proxy_host = swift_config.proxy_host
    proxy_port = swift_config.proxy_port
    use_ssl = swift_config.use_ssl if proxy_host is None else False

    subchecks = []
    subchecks.append(make_tcp_check(
        proxy_host is not None and proxy_host or host,
        proxy_port is not None and proxy_port or port))

    if use_ssl:
        subchecks.append(make_ssl_check(
            proxy_host is not None and proxy_host or host,
            proxy_port is not None and proxy_port or port))

    storage = swiftlib.Swift(auth_host=host,
                             auth_port=port,
                             auth_path=swift_config.auth_path,
                             tenant_name=swift_config.tenant_name,
                             region=swift_config.region,
                             user=swift_config.user,
                             key=swift_config.key,
                             use_ssl=use_ssl,
                             proxy_host=proxy_host,
                             proxy_port=proxy_port,
                             connect_timeout=CONNECT_TIMEOUT,
                             timeout=CONNECT_TIMEOUT)

    @inlineCallbacks
    def check_container(container):
        """Try to connect to AWS and authenticate."""
        f = storage.stat_container(container)
        yield f.deferred

    auth_checks = []
    for name, container in (("container", swift_config.container),
                            ("fallback_container",
                             swift_config.fallback_container)):
        if container:
            auth_checks.append(
                make_check("auth.%s" % name,
                           lambda cname=container: check_container(cname)))
    if auth_checks:
        subchecks.append(parallel_check(auth_checks))
    return add_check_prefix("swift", sequential_check(subchecks))


FACEBOOK_API_HOST = "api.facebook.com"
FACEBOOK_API_PORT = 443


def make_facebook_check():
    """Make a check for accessibility of Facebook APIs."""
    subchecks = []
    subchecks.append(make_tcp_check(FACEBOOK_API_HOST, FACEBOOK_API_PORT))
    subchecks.append(make_ssl_check(FACEBOOK_API_HOST, FACEBOOK_API_PORT))
    check = add_check_prefix("facebook", sequential_check(subchecks))
    return guard_check(check,
                       lambda: config.contact_sync_engine.facebook_enabled)


GOOGLE_CONTACTS_API_HOST = gdata.contacts.client.ContactsClient.server
GOOGLE_CONTACTS_API_PORT = 443


def make_google_check():
    """Make a check for accessibility of Google APIs."""
    subchecks = []

    subchecks.append(make_tcp_check(GOOGLE_CONTACTS_API_HOST,
                                    GOOGLE_CONTACTS_API_PORT))
    subchecks.append(make_ssl_check(GOOGLE_CONTACTS_API_HOST,
                                    GOOGLE_CONTACTS_API_PORT))

    check = add_check_prefix("google", "contacts", sequential_check(subchecks))
    return guard_check(check,
                       lambda: config.contact_sync_engine.google_enabled)


def make_uri_check(uri):
    """Make a uri check for accessibility."""
    uri_components = urlsplit(uri)
    host = uri_components.hostname
    use_ssl = (uri_components.scheme == "https")
    port = uri_components.port or (443 if use_ssl else 80)
    subchecks = []
    subchecks.append(make_tcp_check(host, port))
    if use_ssl:
        subchecks.append(make_ssl_check(host, port))
    return subchecks


def make_sso_check():
    """Make a check for sso accessibility."""
    service_root = get_api_service_root()
    subchecks = make_uri_check(service_root)

    return add_check_prefix('sso', sequential_check(subchecks))


def make_upay_check():
    """Make a check for upay accessibility."""
    service_root, username, password = get_upay_config()
    subchecks = make_uri_check(service_root)

    def check_auth():
        """Try to connect to UPay and authenticate."""
        client = UbuntuPayClient(
            config.upay.consumer_id, username, password, service_root)
        # check conncheck test user preferences
        client.account_preferences('conncheck')

    subchecks.append(make_check("auth", check_auth, blocking=True))
    return add_check_prefix("upay", sequential_check(subchecks))


def make_u1db_internal_check():
    """Check that u1db internal frontend is accessible."""
    subchecks = make_uri_check(config.u1db.internal_fe_server)
    return add_check_prefix("u1db.internal", sequential_check(subchecks))


def trap_ENOENT(fn, default):
    """Call a function and return a default if ENOENT."""
    try:
        return fn()
    except IOError, e:
        if e.errno != errno.ENOENT:
            raise
        return default


def make_memcached_check(option="memcached.servers", prefix="memcached"):
    """Make a check for memcached accessibility."""
    section, option = option.split('.')
    servers = getattr(getattr(config, section), option).split(';')
    servers = [(host, int(port)) for host, port in (server.split(':')
               for server in servers)]

    subchecks = []
    for (host, port), index in izip(servers, xrange(len(servers))):
        subcheck = add_check_prefix(str(index),
                                    make_tcp_check(host, port))
        subchecks.append(subcheck)

    check = parallel_check(subchecks)
    return add_check_prefix(prefix, check)


def make_redis_check(section="redis", prefix="redis"):
    """Make a check for the configured redis server."""
    config_section = getattr(config, section)
    host = config_section.host
    port = config_section.port or BOGUS_PORT
    subchecks = []
    subchecks.append(make_tcp_check(host, port))

    def do_auth():
        """Connect and authenticate."""
        client = redis.client.Redis(host=host, port=port)
        if not client.ping():
            raise RuntimeError("failed to ping redis")

    subchecks.append(make_check("auth", do_auth))
    return add_check_prefix(prefix, sequential_check(subchecks))


@inlineCallbacks
def run_checks(pattern, results):
    """Make and run all the pertinent checks."""
    try:
        subchecks = []

        subchecks.append(make_rabbitmq_check())
        subchecks.append(make_rabbitmq_check(section="matvu_music_harvester",
                                             prefix="matvu.music_harvester"))
        subchecks.append(make_oops_rabbitmq_check())
        subchecks.append(make_statsd_check())
        subchecks.append(make_s3_check())
        subchecks.append(make_swift_check())
        subchecks.append(make_sso_check())
        subchecks.append(make_upay_check())
        subchecks.append(make_u1db_internal_check())

        subchecks.append(make_memcached_check())
        subchecks.append(make_redis_check())

        connection_settings = get_connection_settings()
        for store_name, settings in connection_settings.iteritems():
            subchecks.append(make_db_check(store_name, settings))

        subchecks.append(make_facebook_check())
        subchecks.append(make_google_check())

        all_checks = parallel_check(subchecks)
        yield all_checks.check(pattern, results)
    finally:
        reactor.stop()


class TimestampOutput(object):

    def __init__(self, output):
        self.start = time.time()
        self.output = output

    def write(self, data):
        self.output.write("%.3f: %s" % (time.time() - self.start, data))


class ConsoleOutput(ResultTracker):
    """Displays check results."""

    def __init__(self, output, verbose, show_tracebacks, show_duration):
        """Initialize an instance."""
        super(ConsoleOutput, self).__init__()
        self.__output = output
        self.__verbose = verbose
        self.__show_tracebacks = show_tracebacks
        self.__show_duration = show_duration

    def format_duration(self, duration):
        if not self.__show_duration:
            return ""
        return " (%.3f ms)" % duration

    def notify_start(self, name, info):
        """Register the start of a check."""
        if self.__verbose:
            if info:
                info = " (%s)" % (info,)
            self.__output.write("Starting %s%s...\n" % (name, info or ''))

    def notify_skip(self, name):
        """Register a check being skipped."""
        self.__output.write("SKIPPING %s\n" % (name,))

    def notify_success(self, name, duration):
        """Register a success."""
        self.__output.write("OK %s%s\n" % (
            name, self.format_duration(duration)))

    def notify_failure(self, name, info, exc_info, duration):
        """Register a failure."""
        message = str(exc_info[1]).split("\n")[0]
        if info:
            message = "(%s): %s" % (info, message)
        self.__output.write("FAILED %s%s: %s\n" % (
            name, self.format_duration(duration), message))
        if self.__show_tracebacks:
            formatted = traceback.format_exception(exc_info[0],
                                                   exc_info[1],
                                                   exc_info[2],
                                                   None)
            lines = "".join(formatted).split("\n")
            if len(lines) > 0 and len(lines[-1]) == 0:
                lines.pop()
            indented = "\n".join(["  %s" % (line,) for line in lines])
            self.__output.write("%s\n" % (indented,))


def main(*args):
    """Parse arguments, then build and run checks in a reactor."""
    usage = "Usage: %prog [-c CONFIG_FILE] [PATTERNS...]"
    parser = OptionParser(usage=usage)
    parser.add_option("-v", "--verbose", dest="verbose",
                      action="store_true", default=False,
                      help="Show additional status")
    parser.add_option("-d", "--duration", dest="show_duration",
                      action="store_true", default=False,
                      help="Show duration")
    parser.add_option("-t", "--tracebacks", dest="show_tracebacks",
                      action="store_true", default=False,
                      help="Show tracebacks on failure")

    options, args = parser.parse_args(list(args))

    if len(args) > 0:
        patterns = args
    else:
        patterns = str(config.checks.include).strip()
        if len(patterns) > 0:
            patterns = re.split(r'\s*,\s*', patterns)
        else:
            patterns = ()
    pattern = SumPattern(map(SimplePattern, patterns))

    def make_daemon_thread(*args, **kw):
        """Create a daemon thread."""
        thread = Thread(*args, **kw)
        thread.daemon = True
        return thread

    threadpool = ThreadPool(minthreads=1)
    threadpool.threadFactory = make_daemon_thread
    reactor.threadpool = threadpool
    reactor.callWhenRunning(threadpool.start)

    output = sys.stdout
    if options.show_duration:
        output = TimestampOutput(output)

    results = ConsoleOutput(output=output,
                            show_tracebacks=options.show_tracebacks,
                            show_duration=options.show_duration,
                            verbose=options.verbose)
    results = FailureCountingResultWrapper(results)
    reactor.callWhenRunning(run_checks, pattern, results)

    reactor.run()

    if results.any_failed():
        return 1
    else:
        return 0


exit(main(*sys.argv[1:]))
