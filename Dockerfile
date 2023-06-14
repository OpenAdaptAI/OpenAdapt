# Base image
FROM ubuntu:latest

# Install Git
RUN apt-get update && apt-get install -y git

# Set the working directory
WORKDIR /src

# Copy the OpenAdapt root directory from the host to the container
COPY openadapt /src

# Run the Git diff command
# CMD git diff openadapt/contrib/git_message.py
