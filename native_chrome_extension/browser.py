import nativemessaging

while True:
    message = nativemessaging.get_message()
    print(message)
    if message == "hello":
        nativemessaging.send_message(nativemessaging.encode_message("world"))
