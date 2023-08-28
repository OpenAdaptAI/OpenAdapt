#!/bin/bash

# Set the name of your Native Messaging Host
HOST_NAME=openadapt

# Set the path to your Native Messaging Host manifest file
MANIFEST_PATH=mac_nativehost_file.json

# Check if the systemwide Native Messaging Host directory exists
if [ -d "/Library/Google/Chrome/NativeMessagingHosts/" ]; then
  if [ "$1" == "install" ]; then
    # Copy the Native Messaging Host manifest file to the directory
    sudo cp "$MANIFEST_PATH" "/Library/Google/Chrome/NativeMessagingHosts/$HOST_NAME.json"

    echo "Native Messaging Host installed systemwide successfully!"
  elif [ "$1" == "uninstall" ]; then
    # Remove the Native Messaging Host manifest file from the directory
    sudo rm "/Library/Google/Chrome/NativeMessagingHosts/$HOST_NAME.json"

    echo "Native Messaging Host uninstalled systemwide successfully!"
  else
    echo "Usage: $0 [install|uninstall]"
  fi
fi

# Check if the user Native Messaging Host directory exists
if [ -d "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/" ]; then
  if [ "$1" == "install" ]; then
    # Copy the Native Messaging Host manifest file to the directory
    cp "$MANIFEST_PATH" "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/$HOST_NAME.json"

    echo "Native Messaging Host installed for user successfully!"
  elif [ "$1" == "uninstall" ]; then
    # Remove the Native Messaging Host manifest file from the directory
    rm "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/$HOST_NAME.json"

    echo "Native Messaging Host uninstalled for user successfully!"
  else
    echo "Usage: $0 [install|uninstall]"
  fi
fi
