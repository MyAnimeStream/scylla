import random
import socket
import sys
import traceback
from multiprocessing import Process

from tornado import httpclient, ioloop, iostream, web
from tornado.httpclient import HTTPResponse

from scylla.config import get_config
from scylla.database import ProxyIP
from scylla.loggings import logger

# Using CurlAsyncHTTPClient because its proxy support
httpclient.AsyncHTTPClient.configure("tornado.curl_httpclient.CurlAsyncHTTPClient")


def get_proxy(https=False) -> ProxyIP:
    proxies: [ProxyIP] = ProxyIP.select().where(ProxyIP.is_valid == True).where(ProxyIP.stability >= 0.9)

    if https:
        proxies = proxies.where(ProxyIP.is_https == True)

    proxies = proxies.order_by(ProxyIP.updated_at.desc()).limit(63)
    proxy: ProxyIP = random.choice(proxies)

    return proxy


class ForwardingRequestHandler(web.RequestHandler):
    """
    A very rough ForwardingRequestHandler, only supports HTTP requests.
    """
    SUPPORTED_METHODS = ['GET', 'POST', 'CONNECT']

    def data_received(self, chunk):
        pass

    def get_proxy_and_forward(self):
        https = False

        # At present, this proxy does not support https
        if self.request.uri.startswith('https'):
            https = True

        disable_forward_proxy = get_config('disable_forward_proxy', default=False)

        if disable_forward_proxy:
            self.forward()
        else:
            proxy = get_proxy(https=https)
            self.forward(host=proxy.ip, port=proxy.port)

    @web.asynchronous
    def get(self, *args, **kwargs):
        self.get_proxy_and_forward()

    @web.asynchronous
    def post(self, *args, **kwargs):
        self.get_proxy_and_forward()

    def handle_response(self, response: HTTPResponse):

        if response.body:
            self.write(response.body)
            self.finish()
        elif response.error:
            logger.debug('The forward proxy has an error: {}'.format(response.error))
            self.finish()
        else:
            self.finish()

    def forward(self, host=None, port=None):
        try:
            url = self.request.uri

            body = self.request.body

            if not body:
                body = None

            httpclient.AsyncHTTPClient().fetch(
                httpclient.HTTPRequest(
                    url=url,
                    method=self.request.method,
                    body=body,
                    headers=self.request.headers,
                    follow_redirects=False,
                    validate_cert=False,
                    proxy_host=host,
                    proxy_port=port),
                self.handle_response)

        except httpclient.HTTPError as e:
            logger.debug("tornado signalled HTTPError {}".format(e))
            self.set_status(500)
            self.finish()
        except:
            self.set_status(500)
            self.write("Internal server error:\n" +
                       ''.join(traceback.format_exception(*sys.exc_info())))
            self.finish()

    @web.asynchronous
    def connect(self):
        logger.debug('Start CONNECT to %s', self.request.uri)
        host, port = self.request.uri.split(':')
        client = self.request.connection.stream

        def close_client(data: bytes = None) -> None:
            if upstream.closed():
                return
            if data:
                upstream.write(data)
            upstream.close()

        def upstream_close(data: bytes = None) -> None:
            if client.closed():
                return
            if data:
                client.write(data)
            client.close()

        def start_tunnel():
            logger.debug('CONNECT tunnel established to %s', self.request.uri)
            client.read_until_close(close_client, upstream.write)
            upstream.read_until_close(upstream_close, client.write)
            client.write(b'HTTP/1.0 200 Connection established\r\n\r\n')

        def on_proxy_response(data: bytes = None) -> None:
            if data:
                first_line = data.splitlines()[0]
                http_v, status, text = first_line.split(None, 2)
                if int(status) == 200:
                    logger.debug('Connected to upstream proxy %s', proxy)
                    start_tunnel()
                    return

            self.set_status(500)
            self.finish()

        def start_proxy_tunnel() -> None:
            upstream.write(b'CONNECT %s HTTP/1.1\r\n' % self.request.uri.encode())
            upstream.write(b'Host: %s\r\n' % self.request.uri.encode())
            upstream.write(b'Proxy-Connection: Keep-Alive\r\n\r\n')
            upstream.read_until(b'\r\n\r\n', on_proxy_response)

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        upstream = iostream.IOStream(s)

        proxy = get_proxy(self.request.uri.startswith('https'))
        if proxy:
            upstream.connect((proxy.ip, proxy.port), start_proxy_tunnel)
        else:
            upstream.connect((host, int(port)), start_tunnel)


def make_app():
    return web.Application([
        (r'.*', ForwardingRequestHandler),
    ])


def start_forward_proxy_server():
    app = make_app()
    port = int(get_config('proxy_port', default='8081'))
    app.listen(port)
    logger.info('Start forward proxy server on port {}'.format(port))
    ioloop.IOLoop.current().start()


def start_forward_proxy_server_non_blocking():
    p = Process(target=start_forward_proxy_server, daemon=True)
    p.start()
