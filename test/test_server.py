import socket, time
import mock
from netlib import tcp
from libpathod import pathoc
import tutils, tservers
from libmproxy import flow, proxy

"""
    Note that the choice of response code in these tests matters more than you
    might think. libcurl treats a 304 response code differently from, say, a
    200 response code - it will correctly terminate a 304 response with no
    content-length header, whereas it will block forever waiting for content
    for a 200 response.
"""

class CommonMixin:
    def test_large(self):
        assert len(self.pathod("200:b@50k").content) == 1024*50

    def test_replay(self):
        assert self.pathod("304").status_code == 304
        assert len(self.master.state.view) == 1
        l = self.master.state.view[0]
        assert l.response.code == 304
        l.request.path = "/p/305"
        rt = self.master.replay_request(l, block=True)
        assert l.response.code == 305

        # Disconnect error
        l.request.path = "/p/305:d0"
        rt = self.master.replay_request(l, block=True)
        assert l.error

        # Port error
        l.request.port = 1
        self.master.replay_request(l, block=True)
        assert l.error

    def test_http(self):
        f = self.pathod("304")
        assert f.status_code == 304

        l = self.master.state.view[0]
        assert l.request.client_conn.address
        assert "host" in l.request.headers
        assert l.response.code == 304



class TestHTTP(tservers.HTTPProxTest, CommonMixin):
    def test_app_err(self):
        p = self.pathoc()
        ret = p.request("get:'http://errapp/'")
        assert ret.status_code == 500
        assert "ValueError" in ret.content

    def test_invalid_http(self):
        t = tcp.TCPClient("127.0.0.1", self.proxy.port)
        t.connect()
        t.wfile.write("invalid\n\n")
        t.wfile.flush()
        assert "Bad Request" in t.rfile.readline()

    def test_invalid_connect(self):
        t = tcp.TCPClient("127.0.0.1", self.proxy.port)
        t.connect()
        t.wfile.write("CONNECT invalid\n\n")
        t.wfile.flush()
        assert "Bad Request" in t.rfile.readline()

    def test_upstream_ssl_error(self):
        p = self.pathoc()
        ret = p.request("get:'https://localhost:%s/'"%self.server.port)
        assert ret.status_code == 400

    def test_connection_close(self):
        # Add a body, so we have a content-length header, which combined with
        # HTTP1.1 means the connection is kept alive.
        response = '%s/p/200:b@1'%self.server.urlbase

        # Lets sanity check that the connection does indeed stay open by
        # issuing two requests over the same connection
        p = self.pathoc()
        assert p.request("get:'%s'"%response)
        assert p.request("get:'%s'"%response)

        # Now check that the connection is closed as the client specifies
        p = self.pathoc()
        assert p.request("get:'%s':h'Connection'='close'"%response)
        tutils.raises("disconnect", p.request, "get:'%s'"%response)

    def test_reconnect(self):
        req = "get:'%s/p/200:b@1:da'"%self.server.urlbase
        p = self.pathoc()
        assert p.request(req)
        # Server has disconnected. Mitmproxy should detect this, and reconnect.
        assert p.request(req)
        assert p.request(req)

        # However, if the server disconnects on our first try, it's an error.
        req = "get:'%s/p/200:b@1:d0'"%self.server.urlbase
        p = self.pathoc()
        tutils.raises("server disconnect", p.request, req)

    def test_proxy_ioerror(self):
        # Tests a difficult-to-trigger condition, where an IOError is raised
        # within our read loop.
        with mock.patch("libmproxy.proxy.ProxyHandler.read_request") as m:
            m.side_effect = IOError("error!")
            tutils.raises("server disconnect", self.pathod, "304")

    def test_get_connection_switching(self):
        def switched(l):
            for i in l:
                if "switching" in i:
                    return True
        req = "get:'%s/p/200:b@1'"
        p = self.pathoc()
        assert p.request(req%self.server.urlbase)
        assert p.request(req%self.server2.urlbase)
        assert switched(self.proxy.log)

    def test_get_connection_err(self):
        p = self.pathoc()
        ret = p.request("get:'http://localhost:0'")
        assert ret.status_code == 502


class TestHTTPS(tservers.HTTPProxTest, CommonMixin):
    ssl = True
    clientcerts = True
    def test_clientcert(self):
        f = self.pathod("304")
        assert self.server.last_log()["request"]["clientcert"]["keyinfo"]

    def test_sni(self):
        pass


class TestHTTPSCertfile(tservers.HTTPProxTest, CommonMixin):
    ssl = True
    certfile = True
    def test_certfile(self):
        assert self.pathod("304")


class TestReverse(tservers.ReverseProxTest, CommonMixin):
    reverse = True


class TestTransparent(tservers.TransparentProxTest, CommonMixin):
    transparent = True
    ssl = False


class TestTransparentSSL(tservers.TransparentProxTest, CommonMixin):
    transparent = True
    ssl = True


class TestProxy(tservers.HTTPProxTest):
    def test_http(self):
        f = self.pathod("304")
        assert f.status_code == 304

        l = self.master.state.view[0]
        assert l.request.client_conn.address
        assert "host" in l.request.headers
        assert l.response.code == 304

    def test_response_timestamps(self):
        # test that we notice at least 2 sec delay between timestamps
        # in response object
        f = self.pathod("304:b@1k:p50,1")
        assert f.status_code == 304

        response = self.master.state.view[0].response
        assert 1 <= response.timestamp_end - response.timestamp_start <= 1.2

    def test_request_timestamps(self):
        # test that we notice a delay between timestamps in request object
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.connect(("127.0.0.1", self.proxy.port))

        # call pathod server, wait a second to complete the request
        connection.send("GET http://localhost:%d/p/304:b@1k HTTP/1.1\r\n"%self.server.port)
        connection.send("\r\n");
        connection.recv(50000)
        connection.close()

        request, response = self.master.state.view[0].request, self.master.state.view[0].response
        assert response.code == 304  # sanity test for our low level request
        assert request.timestamp_end - request.timestamp_start > 0

    def test_request_timestamps_not_affected_by_client_time(self):
        # test that don't include user wait time in request's timestamps

        f = self.pathod("304:b@10k")
        assert f.status_code == 304
        f = self.pathod("304:b@10k")
        assert f.status_code == 304

        request = self.master.state.view[0].request
        assert request.timestamp_end - request.timestamp_start <= 0.1

        request = self.master.state.view[1].request
        assert request.timestamp_end - request.timestamp_start <= 0.1



class MasterFakeResponse(tservers.TestMaster):
    def handle_request(self, m):
        resp = tutils.tresp()
        m.reply(resp)


class TestFakeResponse(tservers.HTTPProxTest):
    masterclass = MasterFakeResponse
    def test_kill(self):
        p = self.pathoc()
        f = self.pathod("200")
        assert "header_response" in f.headers.keys()



class MasterKillRequest(tservers.TestMaster):
    def handle_request(self, m):
        m.reply(proxy.KILL)


class TestKillRequest(tservers.HTTPProxTest):
    masterclass = MasterKillRequest
    def test_kill(self):
        p = self.pathoc()
        tutils.raises("server disconnect", self.pathod, "200")
        # Nothing should have hit the server
        assert not self.server.last_log()


class MasterKillResponse(tservers.TestMaster):
    def handle_response(self, m):
        m.reply(proxy.KILL)


class TestKillResponse(tservers.HTTPProxTest):
    masterclass = MasterKillResponse
    def test_kill(self):
        p = self.pathoc()
        tutils.raises("server disconnect", self.pathod, "200")
        # The server should have seen a request
        assert self.server.last_log()

