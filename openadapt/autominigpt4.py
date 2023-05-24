"""
Automate installation of MiniGPT-4

Usage: python openadapt/autominigpt4.py
"""

import subprocess
import os
import sys
import psutil


def clone_repository(repo_url, target_dir):
    try:
        subprocess.check_call(['git', 'clone', repo_url, target_dir])
        print("Repository cloned successfully!")
    except subprocess.CalledProcessError as e:
        print("Failed to clone repository:", e)
        sys.exit()


def clone_repository_skip_lfs(repo_url, target_dir):
    try:
        # Clone the repository with a shallow clone and enable sparse checkout
        subprocess.check_call(
            ['git', 'clone', '--depth', '1', '--no-checkout', repo_url,
             target_dir])
        os.chdir(target_dir)
        # Enable sparse checkout
        subprocess.check_call(['git', 'sparse-checkout', 'init', '--cone'])
        # Set the sparse-checkout pattern to include all files
        subprocess.check_call(['git', 'sparse-checkout', 'set', '/*'])
        # Set the GIT_LFS_SKIP_SMUDGE environment variable to skip
        os.environ['GIT_LFS_SKIP_SMUDGE'] = '1'
        # Perform the checkout to download the non-LFS files
        subprocess.check_call(['git', 'checkout'])
        # Remove the GIT_LFS_SKIP_SMUDGE environment variable
        del os.environ['GIT_LFS_SKIP_SMUDGE']
        print("Repository cloned successfully without downloading LFS files!")
    except subprocess.CalledProcessError as e:
        print("Failed to clone repository:", e)
        sys.exit()


def check_and_clone(repository_url, target_directory, lfs):
    if os.path.exists(target_directory):
        os.chdir(target_directory)
        if not is_valid_git_repository(target_directory):
            print(
                f"Directory '{target_directory}' exists but is not a valid Git repository. Please "
                f"check the files in this directory and choose whether to delete the directory. "
                f"Only restart the installation process if you have deleted or renamed this "
                f"directory.")
            sys.exit()
        else:
            print(
                f"Repository '{target_directory}' already exists and is valid. Skipping cloning "
                f"step.")
    else:
        if not lfs:
            clone_repository(repository_url, target_directory)
        else:
            clone_repository_skip_lfs(repository_url, target_directory)


def is_valid_git_repository(target_directory):
    try:
        output = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'])
        git_toplevel = output.decode().strip()
        git_toplevel = os.path.normpath(git_toplevel)
        target_directory = os.path.normpath(target_directory)
        return git_toplevel == target_directory
    except subprocess.CalledProcessError:
        return False


def download_lfs_files(repo_dir):
    try:
        subprocess.check_call(['git', 'lfs', 'fetch'])
        subprocess.check_call(['git', 'lfs', 'checkout'])
        print(f"LFS files downloaded to {repo_dir} successfully!")
    except subprocess.CalledProcessError as e:
        print(f"Failed to download LFS files to {repo_dir}:", e)
        sys.exit()


def check_ram_info():
    ram_info = psutil.virtual_memory()
    total_ram = ram_info.total / (1024 ** 3)  # Convert to GB
    print("Total RAM: {:.2f} GB".format(total_ram))
    return total_ram


if __name__ == "__main__":
    # clone MiniGPT-4
    minigpt4 = os.path.abspath('MiniGPT4')
    # Check if the repository already exists
    check_and_clone('https://github.com/Vision-CAIR/MiniGPT-4', minigpt4, False)

    os.chdir(minigpt4)

    # install git lfs
    try:
        subprocess.check_call(['git', 'lfs', 'install'])
        print("Git LFS installed successfully!")
    except subprocess.CalledProcessError as e:
        print("Failed to install Git LFS:", e)

    # clone vicuna 13b v1.1 weights without the lfs files
    vicuna = os.path.join(minigpt4, 'vicuna-13b-delta-v1.1')
    check_and_clone('https://huggingface.co/lmsys/vicuna-13b-delta-v1.1', vicuna, True)
    os.chdir(vicuna)
    # download the complete large size files
    download_lfs_files(vicuna)
    os.chdir(minigpt4)

    # clone llama 13b weights
    llama = os.path.join(minigpt4, 'llama-13b')
    check_and_clone('https://huggingface.co/huggyllama/llama-13b', llama, True)
    os.chdir(llama)
    # download the complete large size files
    download_lfs_files(llama)

    # pip3 install fschat
    # install FastChat
    try:
        subprocess.check_call(['pip3', 'install', 'fschat'])
        print("FastChat installed successfully!")
    except subprocess.CalledProcessError as e:
        print("Failed to install FastChat:", e)

    # create working weight
    target = os.path.join(minigpt4, 'vicuna')
    # First check how much RAM there is. if less than 60GB, we use --low-cpu-mem.
    try:
        if check_ram_info() < 60:
            subprocess.check_call(['python', '-m', 'fastchat.model.apply_delta', '--base-model-path',
                                   llama, '--target-model-path', target, '--delta-path', vicuna,
                                   '--low-cpu-mem'])
        else:
            subprocess.check_call(
                ['python', '-m', 'fastchat.model.apply_delta', '--base-model-path',
                 llama, '--target-model-path', target, '--delta-path', vicuna])
        print("Working weight created successfully!")
    except subprocess.CalledProcessError as e:
        print("Failed to create working weight:", e)

    # Then, set the path to the vicuna weight in the model config file here at Line 16.
    file1 = os.path.join(minigpt4, 'minigpt4', 'configs', 'models', 'minigpt4.yaml')
    with open(file1, "r") as file:
        # Read the contents of the file
        file_contents = file.read()

    modified_contents = file_contents.replace("/path/to/vicuna/weights/", target)

    with open(file1, "w") as file:
        # Write the modified contents to the file
        file.write(modified_contents)

    # Then, set the path to the pretrained checkpoint in the evaluation
    #  config file in eval_configs/minigpt4_eval.yaml at Line 11.
    checkpoint_dir = os.path.abspath('minigpt4_checkpoint')
    checkpoint = os.path.join(checkpoint_dir, 'pretrained_minigpt4.pth')
    file2 = os.path.join(minigpt4, 'eval_configs', 'minigpt4_eval.yaml')
    with open(file2, "r") as file:
        # Read the contents of the file
        file_contents = file.read()

    modified_contents = file_contents.replace("/path/to/pretrained/ckpt/", checkpoint)

    with open(file2, "w") as file:
        # Write the modified contents to the file
        file.write(modified_contents)
