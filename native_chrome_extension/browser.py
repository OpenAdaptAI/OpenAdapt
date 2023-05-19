""" Module for the browser native messaging host. """
import nativemessaging


def main() -> None:
    """
    Main function for the browser native messaging host.
    """
    reply_num = 0
    while True:
        message = nativemessaging.get_message()
        print(message)
        nativemessaging.send_message(nativemessaging.encode_message(str(reply_num)))
        reply_num += 1


if __name__ == "__main__":
    main()
