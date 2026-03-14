"""Publish/Subscribe tests."""
import time
import pytest
import nng


URL = "inproc://test_pubsub"


def _pub_sub_pair(url_suffix=""):
    url = URL + url_suffix
    pub = nng.PubSocket()
    sub = nng.SubSocket()
    sub.recv_timeout = 2000
    pub.add_listener(url).start()
    sub.add_dialer(url).start()
    return pub, sub


def test_subscribe_all():
    """Empty prefix subscribes to everything."""
    with nng.PubSocket() as pub, nng.SubSocket() as sub:
        sub.recv_timeout = 2000
        pub.add_listener(URL).start()
        sub.add_dialer(URL).start()
        sub.subscribe(b"")   # all topics

        time.sleep(0.02)     # let the pipe establish
        pub.send(b"hello")
        assert sub.recv() == b"hello"


def test_subscribe_prefix():
    """Only messages matching the topic prefix are delivered."""
    with nng.PubSocket() as pub, nng.SubSocket() as sub:
        sub.recv_timeout = 2000
        pub.add_listener(URL + "_prefix").start()
        sub.add_dialer(URL + "_prefix").start()
        sub.subscribe(b"news.")

        time.sleep(0.02)
        pub.send(b"news.sport match result")
        assert sub.recv() == b"news.sport match result"


def test_subscribe_prefix_filtered():
    """Messages not matching the prefix are silently dropped."""
    with nng.PubSocket() as pub, nng.SubSocket() as sub:
        sub.recv_timeout = 200   # short timeout – expect NngTimeout
        pub.add_listener(URL + "_filt").start()
        sub.add_dialer(URL + "_filt").start()
        sub.subscribe(b"A.")     # only "A." messages

        time.sleep(0.02)
        pub.send(b"B.message")   # should be filtered out

        with pytest.raises(nng.NngTimeout):
            sub.recv()


def test_unsubscribe():
    with nng.PubSocket() as pub, nng.SubSocket() as sub:
        sub.recv_timeout = 200
        pub.add_listener(URL + "_unsub").start()
        sub.add_dialer(URL + "_unsub").start()
        sub.subscribe(b"topic.")

        time.sleep(0.02)
        pub.send(b"topic.first")
        assert sub.recv() == b"topic.first"

        sub.unsubscribe(b"topic.")
        time.sleep(0.02)
        pub.send(b"topic.second")

        with pytest.raises(nng.NngTimeout):
            sub.recv()


def test_multiple_subscribers():
    """One publisher, two subscribers each with different topics."""
    with (nng.PubSocket() as pub,
          nng.SubSocket() as sub1,
          nng.SubSocket() as sub2):
        sub1.recv_timeout = sub2.recv_timeout = 2000
        pub.add_listener(URL + "_multi").start()
        sub1.add_dialer(URL + "_multi").start()
        sub2.add_dialer(URL + "_multi").start()
        sub1.subscribe(b"a.")
        sub2.subscribe(b"b.")

        time.sleep(0.02)
        pub.send(b"a.hello")
        pub.send(b"b.world")

        assert sub1.recv() == b"a.hello"
        assert sub2.recv() == b"b.world"
