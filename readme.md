# Scrubbing Sensitive Information from Keyboard Input
## Introduction
This pull request is a proof-of-concept for scrubbing [PHI](https://www.dhcs.ca.gov/dataandstats/data/Pages/ListofHIPAAIdentifiers.aspx) text-based identifiers via keyboard input.

## Scrubbing Method
### Input Approach to Scrubbing
In this pull request, I implemented scrubbing of text data from the keyboard input. This approach is also based on the assumption that the user is not using a password manager, and is entering sensitive information via keyboard input.


It is important to acknowledge the private information that would come from the screen input. For this approach, I would use computer vision libraries, and built-in Python libraries to scrub the image data, and non-typed text data, such as Number Recognition from PyPi.

### Input Events
One keyboard press (eg. "a") is considered an individual input event. Thus, in order to scrub the text, we need to consider input events collectively. The `SCRUBBING_BUFFER_SIZE` variable denotes the number of consecutive keyboard events that will be considered as a collective. Keyboard characters are then appended and displayed in kebab case in the HTML screenshots. 

The `SCRUBBING_BUFFER_SIZE` is originally set to 75, to accomodate large text inputs (eg. long email addresses) but also to be mindful of performance.

Another approach considered was treating keyboard input between two mouse/screen events as a collective. However, this approach was not implemented due to the human use. When entering information, the user could forget the rest of their credit card number and have to reference another source, or get interrupted, causing the collective event of entering sensitive information (eg. an email address) to be separated.

At this point, libraries likes Regex can be used to generate an "expression" format for each type of sensitive information. If sensitive information is detected, the series of keyboard characters are removed.

### Time of Scrubbing
This implementation scrubbed the text data in the visualize.py, which allows the user to store their data locally, and scrub the data before sending it to a centralized server. This approach prevents the gathering of the data process from slowing down.

An alternative approach would be to scrub the sensitive information before storing it locally. This approach would be more secure, and would prevent the user from accidentally sending sensitive information to the server. 

## Requirements and File Architecture
### New Imports
```
import re
```




### Instructions
This PR automatically generates recordings with scrubbing on, and the established record/visualize commands can be run after OpenAdapt.AI setup:

```
python -m puterbot.record "testing out puterbot"
python -m puterbot.visualize
```


## Results
For this proof-of-concept, OpenAdapt is capable of detecting the character "1" and phone numbers. Testing was done by applying the scrubbing feature.

![Filter_off (1)](https://user-images.githubusercontent.com/97775581/236723220-482203be-3f0f-4c86-8aee-2ab0c3a8700d.gif)
Figure 1. Scrubbing filter off. The phone number is still visible.


![Filter_on (1)](https://user-images.githubusercontent.com/97775581/236723129-0b8641ee-534b-440e-89da-7db6a7ae2e76.gif)
Figure 2. Scrubbing filter on. The phone number is no longer visible.


## Further Considerations
This method can be scaled to other textual and keyboard-inputted forms of sensitive information (like using the "@" character with ".com" (and other TLDs) to detect email addresses). The following are some other use cases to be addressed, in addition to PII/PHI scrubbing from screen input:
* password detection
* sharing workflows that have sensitive information (eg. like showing someone how to do their taxes): allowing the user to automatically replace the scrubbed sensitive information with dummy variables (eg. "john.doe@em<span>ail.com</span>" for an email address)

## Next Steps
- [ ] optimize for speed and performance (2 for loops)
- [ ] add support for editing keyboard input (eg. backspace, moving the mouse to a different location and entering in text from there)



## Conclusion
Overall, this pull request presents a proof-of-concept for scrubbing text-based sensitive information via keyboard input. The proposed approach considers consecutive keyboard events as a collective to scrub the sensitive information. The implementation of the scrubbing feature was done in the visualize.py file to prevent the gathering of data process from slowing down. The results show that the implementation can detect phone numbers and the character "1." This method can be extended to detect other forms of sensitive information like email addresses and passwords. Further improvements can be made to optimize the code for speed and performance and add support for editing keyboard input.










