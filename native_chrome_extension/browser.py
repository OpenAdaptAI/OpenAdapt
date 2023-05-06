import json
import nativemessaging

def main():
    messages = []

    while True:
        # Receive a message from the Chrome Extension
        message = nativemessaging.get_message()
        if not message:
            break

        # Parse the message as JSON
        message_data = json.loads(message)

        # Store the message in a list
        messages.append(message_data)

    # Do something with the collected messages
    print(messages)

if __name__ == "__main__":
    main()
