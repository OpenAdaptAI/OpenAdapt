import requests
import json

# Define the URL of your application.
# Replace '<your_app_url>' with the actual URL of your app.
url = 'ap-ffRiFfkFoaU8RHKMiFbkzy'

# Define the data you want to send in the body of your POST request.
# This will depend on what your function expects as input.
# Replace '<input_data>' with the actual data you want to send.
data = {
    'input_data': '<input_data>'
}

# Send the POST request and get the response.
response = requests.post(url, json=data)

# Print the status code and the returned data.
print('Status code:', response.status_code)
print('Returned data:', response.json())
