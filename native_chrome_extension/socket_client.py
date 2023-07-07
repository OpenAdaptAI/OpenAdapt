from multiprocessing.connection import Client

from loguru import logger

import time

SERVER_SENDS = True
PORT = 6001

if __name__ == "__main__":
    address = ('localhost', PORT)
    conn = Client(address, authkey=b'secret password')
    while True:
        if SERVER_SENDS:
            logger.info(f"waiting for message...")
            msg = conn.recv()
            logger.info(f"{msg=}")
        else:        
            t = time.time()
            logger.info(f"sending {t=}")
            conn.send(t)
            time.sleep(1)
    # can also send arbitrary objects:
    # conn.send(['a', 2.5, None, int, sum])
    conn.close()
