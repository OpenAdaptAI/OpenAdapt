from multiprocessing.connection import Client
from loguru import logger
from openadapt import config, sockets
import time

RETRY_INTERVAL=5

def main():
    conn = Client((config.SOCKET_ADDRESS,config.SOCKET_PORT) , authkey=config.SOCKET_AUTHKEY)
    while True:
        try:
            if SERVER_SENDS:
                logger.info("Waiting for message...")
                msg = conn.recv()
                logger.info(f"{msg=}")
            else:
                t = time.time()
                logger.info(f"Sending {t=}")
                conn.send(t)
                time.sleep(1)
        except EOFError:
            logger.warning("Connection closed. Reconnecting...")
            while True:
                try:
                    conn = establish_connection()
                    break
                except Exception as exc:
                    logger.warning(f"Failed to reconnect: {exc}")
                    time.sleep(RETRY_INTERVAL)
        except Exception as exc:
            logger.warning(f"Error during communication: {exc}")
            time.sleep(RETRY_INTERVAL)

    conn.close()


if __name__ == "__main__":
    main()
