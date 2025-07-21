import socket
import json
import struct
import binascii
import hashlib
import time
import sys
import threading

HOST = 'stratum-na.rplant.xyz'  # Change to your pool
PORT = 7022                    # Change to your port
USERNAME = 'mbc1qe3flkq8el48edhzudr75a2pj0vdf4uvykdygxg.trial'  # Change to your worker
PASSWORD = 'x'
NUM_THREADS = 32               # Adjust threads here

lock = threading.Lock()       # To protect socket writes

def create_tcp_connection(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(30)
    sock.connect((host, port))
    return sock

def send_line(sock, line):
    with lock:
        sock.sendall((line + '\n').encode())

def receive_lines(sock):
    buffer = ""
    while True:
        chunk = sock.recv(4096).decode()
        if not chunk:
            raise ConnectionError("Connection closed")
        buffer += chunk
        while '\n' in buffer:
            line, buffer = buffer.split('\n', 1)
            yield line.strip()

def power2b_hash(data):
    # TODO: Implement real power2b hash here.
    # Placeholder: double sha256 (not correct for power2b)
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

def bits_to_target(bits_hex):
    bits = int(bits_hex, 16)
    exponent = bits >> 24
    mantissa = bits & 0xffffff
    return mantissa << (8 * (exponent - 3))

class MiningJob:
    def __init__(self, job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime):
        self.job_id = job_id
        self.prevhash = prevhash
        self.coinb1 = coinb1
        self.coinb2 = coinb2
        self.merkle_branch = merkle_branch
        self.version = version
        self.nbits = nbits
        self.ntime = ntime

def calculate_merkle_root(coinbase_hash, merkle_branch):
    current_hash = coinbase_hash
    for h in merkle_branch:
        branch_hash = binascii.unhexlify(h)[::-1]
        combined = current_hash + branch_hash
        current_hash = power2b_hash(combined)
    return current_hash[::-1]

def build_coinbase(coinb1, extranonce1, extranonce2, coinb2):
    return binascii.unhexlify(coinb1) + binascii.unhexlify(extranonce1) + binascii.unhexlify(extranonce2) + binascii.unhexlify(coinb2)

def build_block_header(job, merkle_root, nonce):
    version = struct.pack("<I", int(job.version, 16))
    prevhash = binascii.unhexlify(job.prevhash)[::-1]
    ntime = struct.pack("<I", int(job.ntime, 16))
    nbits = binascii.unhexlify(job.nbits)[::-1]
    nonce_bytes = struct.pack("<I", nonce)
    return version + prevhash + merkle_root + ntime + nbits + nonce_bytes

def mine_worker(sock, job, extranonce1, extranonce2_size, username, thread_id):
    extranonce2_counter = thread_id * 1000000  # offset per thread
    target = bits_to_target(job.nbits)
    print(f"[Thread-{thread_id}] Mining job {job.job_id} with target {hex(target)}")

    while True:
        extranonce2 = f"{extranonce2_counter:0{extranonce2_size * 2}x}"
        coinbase = build_coinbase(job.coinb1, extranonce1, extranonce2, job.coinb2)
        coinbase_hash = power2b_hash(coinbase)
        merkle_root = calculate_merkle_root(coinbase_hash, job.merkle_branch)

        nonce = 0
        while nonce < 0xFFFFFFFF:
            header = build_block_header(job, merkle_root, nonce)
            hash_result = power2b_hash(header)
            hash_int = int.from_bytes(hash_result[::-1], 'big')

            if hash_int < target:
                print(f"[Thread-{thread_id}] Share found! Nonce: {nonce}, extranonce2: {extranonce2}")
                submit = {
                    "id": 4,
                    "method": "mining.submit",
                    "params": [username, job.job_id, extranonce2, job.ntime, f"{nonce:08x}"]
                }
                send_line(sock, json.dumps(submit))
                break

            nonce += 1

        extranonce2_counter += 1

def main():
    sock = create_tcp_connection(HOST, PORT)
    lines = receive_lines(sock)

    # Subscribe
    subscribe = {
        "id": 1,
        "method": "mining.subscribe",
        "params": ["python-power2b-miner/1.0"]
    }
    send_line(sock, json.dumps(subscribe))

    # Authorize
    authorize = {
        "id": 2,
        "method": "mining.authorize",
        "params": [USERNAME, PASSWORD]
    }
    send_line(sock, json.dumps(authorize))

    extranonce1 = None
    extranonce2_size = None
    current_job = None
    threads = []

    try:
        for line in lines:
            data = json.loads(line)
            if "method" in data:
                if data["method"] == "mining.set_difficulty":
                    print("Difficulty set:", data["params"][0])
                elif data["method"] == "mining.notify":
                    params = data["params"]
                    job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, clean_jobs = params
                    current_job = MiningJob(job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime)
                    print(f"New job: {job_id}")

                elif data["method"] == "mining.set_extranonce":
                    extranonce1 = data["params"][0]
                    extranonce2_size = data["params"][1]
                    print(f"Set extranonce1: {extranonce1}, size: {extranonce2_size}")

            elif "id" in data:
                if data["id"] == 1 and "result" in data:
                    extranonce1 = data["result"][1]
                    extranonce2_size = data["result"][2]
                    print(f"Subscribed! extranonce1: {extranonce1}, extranonce2_size: {extranonce2_size}")
                elif data["id"] == 2:
                    if data.get("result") is True:
                        print("Authorized!")
                    else:
                        print("Authorization failed")
                        sys.exit(1)
                elif data["id"] == 4:
                    if data.get("result") is True:
                        print("Share accepted")
                    else:
                        print("Share rejected")

            # Start mining threads after we have job and extranonce info
            if current_job and extranonce1 and extranonce2_size and not threads:
                for i in range(NUM_THREADS):
                    t = threading.Thread(target=mine_worker, args=(sock, current_job, extranonce1, extranonce2_size, USERNAME, i))
                    t.daemon = True
                    t.start()
                    threads.append(t)

    except KeyboardInterrupt:
        print("Mining stopped by user")
    finally:
        sock.close()

if __name__ == "__main__":
    main()
