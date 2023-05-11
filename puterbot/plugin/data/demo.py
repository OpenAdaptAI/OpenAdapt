from pynput.keyboard import Key, Controller
from dotenv import load_dotenv
import os
import openai

load_dotenv()

openai.api_key = os.getenv("API_KEY")

params = {
  'engine': 'text-davinci-003', 
  'prompt': '', 
  'max_tokens': 496, 
  'temperature': 0.7, 
}

endpoint = "localhost:3333"

keyboard = Controller()

def sendkeys(args):
    # open sublime in new terminal window
    os.system("open /Applications/Sublime\ Text.app")

    params.update({"prompt": args})
    response = openai.Completion.create(**params)

    print("Sending keys: ", response["choices"][0]["text"])
    keyboard.type(response["choices"][0]["text"])
    return 200

def test():
    a = sendkeys("Hello, world!")
    if a == 200:
        print("Success!")

if __name__ == "__main__":
    print("api key: ", os.getenv("API_KEY"))
    test()