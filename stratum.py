import json
import socket
import threading
import time
import Queue as queue

class Processor(threading.Thread):

    def __init__(self):
        self.shared = None
        threading.Thread.__init__(self)
        self.daemon = True
        self.request_queue = queue.Queue()
        self.response_queue = queue.Queue()
        self.id_session = {}

    def push_response(self, item):
        self.response_queue.put(item)

    def pop_response(self):
        return self.response_queue.get()

    def push_request(self, session, item):
        self.request_queue.put((session,item))

    def pop_request(self):
        return self.request_queue.get()

    def run(self):
        if self.shared is None:
            raise TypeError("self.shared not set in Processor")
        while not self.shared.stopped():
            session, request = self.pop_request()

            method = request['method']
            params = request.get('params',[])

            if method == 'numblocks.subscribe':
                session.subscribe_to_numblocks()

            elif method == 'address.subscribe':
                address = params[0]
                session.subscribe_to_address(address)

            elif method == 'server.peers':
                session.subscribe_to_peers()

            message_id = request['id']
            self.id_session[message_id] = session
            self.process(request)

        self.stop()

    def stop(self):
        pass

    def process(self, request):
        print "New request", request
        # Do stuff...
        # response = request
        # When ready, you call
        # self.push_response(response)



class Session:

    def __init__(self, connection, address):
        self._connection = connection
        self.address = address
        self._stopped = False
        self.lock = threading.Lock()
        self.numblocks_sub = None
        self.addresses_sub = {}
        print "new session", address

    def stop(self):
        self._connection.close()
        print "Terminating connection:", self.address[0]
        with self.lock:
            self._stopped = True

    def stopped(self):
        with self.lock:
            return self._stopped

    def connection(self):
        if self.stopped():
            raise Exception("Session was stopped")
        else:
            return self._connection

    def subscribe_to_numblocks(self):
        with self.lock:
            self.numblocks_sub = True
    
    def subscribe_to_peers(self):
        pass

    def subscribe_to_address(self,address):
        with self.lock:
            self.addresses_sub[address] = 'unknown'


class TcpResponder(threading.Thread):

    def __init__(self, shared, processor, server):
        self.shared = shared
        self.processor = processor
        self.server = server
        threading.Thread.__init__(self)


    def run(self):
        while not self.shared.stopped():
            response = self.processor.pop_response()
            # if it is a subscription, find the list of sessions that suuscribed
            
            # if there is an id, there should be a session
            # note: I must add and remove the session id to the message id..

            message_id = response.get('id')
            try:
                method = response['method']
            except:
                print "no method", response
                continue

            if message_id:
                session = self.processor.id_session.pop(message_id)
                self.send_response(response, session)

            elif method == 'numblocks.subscribe':
                for session in self.server.sessions:
                    if not session.stopped():
                        if session.numblocks_sub:
                            self.send_response(response, session)

            elif method == 'address.subscribe':
                for session in self.server.sessions:
                    if not session.stopped():
                        addr = response['params'][0]
                        last_status = session.addresses_sub.get(addr)
                        if last_status:
                            new_status = response.get('result')
                            if new_status != last_status:
                                session.addresses_sub[addr] = new_status
                                self.send_response(response, session)
            else:
                print "error", response


    def send_response(self, response, session):
        raw_response = json.dumps(response)
        # Possible race condition here by having session
        # close connection?
        # I assume Python connections are thread safe interfaces
        try:
            connection = session.connection()
            connection.send(raw_response + "\n")
        except:
            session.stop()

class TcpClientRequestor(threading.Thread):

    def __init__(self, shared, processor, session):
        self.shared = shared
        self.processor = processor
        self.message = ""
        self.session = session
        threading.Thread.__init__(self)

    def run(self):
        while not self.shared.stopped():
            if not self.update():
                break

            while self.parse():
                pass

    def update(self):
        data = self.receive()
        if not data:
            # close_session
            self.session.stop()
            return False

        self.message += data
        return True

    def receive(self):
        try:
            return self.session.connection().recv(1024)
        except socket.error:
            return ''

    def parse(self):
        raw_buffer = self.message.find('\n')
        if raw_buffer == -1:
            return False

        raw_command = self.message[0:raw_buffer].strip()
        self.message = self.message[raw_buffer + 1:]
        if raw_command == 'quit': 
            self.session.stop()
            return False

        try:
            command = json.loads(raw_command)
        except:
            self.processor.push_response({"error": "bad JSON", "request": raw_command})
            return True

        try:
            # Try to load vital fields, and return an error if
            # unsuccessful.
            message_id = command['id']
            method = command['method']
        except KeyError:
            # Return an error JSON in response.
            self.processor.push_response({"error": "syntax error", "request": raw_command})
        else:
            self.processor.push_request(self.session,command)

        return True

class TcpServer(threading.Thread):

    def __init__(self, shared, processor, host, port):
        self.shared = shared
        self.processor = processor
        self.sessions = []
        threading.Thread.__init__(self)
        self.daemon = True
        self.host = host
        self.port = port
        self.lock = threading.Lock()

    def run(self):
        print "TCP server started."
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(1)
        responder = TcpResponder(self.shared, self.processor, self)
        responder.start()
        while not self.shared.stopped():
            session = Session(*sock.accept())
            client_req = TcpClientRequestor(self.shared, self.processor, session)
            client_req.start()
            self.add_session(session)
            self.collect_garbage()

    def add_session(self, session):
        with self.lock:
            self.sessions.append(session)

    def collect_garbage(self):
        # Deep copy entire sessions list and blank it
        # This is done to minimise lock contention
        with self.lock:
            sessions = self.sessions[:]
            self.sessions = []
        for session in sessions:
            if not session.stopped():
                # If session is still alive then re-add it back
                # to our internal register
                self.add_session(session)


class Shared:

    def __init__(self):
        self.lock = threading.Lock()
        self._stopped = False

    def stop(self):
        print "Stopping Stratum"
        with self.lock:
            self._stopped = True

    def stopped(self):
        with self.lock:
            return self._stopped

class Stratum:

    def start(self, processor):
        shared = Shared()
        # Bind shared to processor since constructor is user defined
        processor.shared = shared
        processor.start()
        # Create various transports we need
        transports = TcpServer(shared, processor, "176.31.24.241", 50001),
        for server in transports:
            server.start()
        while not shared.stopped():
            if raw_input() == "quit":
                shared.stop()
            time.sleep(1)

if __name__ == "__main__":
    processor = Processor()
    app = Stratum()
    app.start(processor)
