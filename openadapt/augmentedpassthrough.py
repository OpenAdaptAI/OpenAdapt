import subprocess

def run_command_with_logs(cmd):
    # This function will start the command and print its output in real-time
    with subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        try:
            # Print the logs in real-time
            for line in proc.stdout:
                print(line, end='')

            # Wait until process finishes and get the exit code
            return proc.wait()

        except KeyboardInterrupt:
            # Send Ctrl+C signal to the process
            proc.terminate()
            proc.wait()
            # Now delete the passthrough system and exit
            delete_passthrough()
            exit()

def delete_passthrough():
    print("\nDeleting the passthrough system...")
    subprocess.run('net use y: /delete', shell=True)
    print("Passthrough system deleted. Exiting.")

def main():
    try:
        print("Starting the passthrough system...")
        run_command_with_logs('net use y: \\\\passthrough\\C$\\Users')
        
        print("Passthrough system started. Press Ctrl+C to stop.")
        try:
            while True:
                pass
        except KeyboardInterrupt:
            delete_passthrough()
        
    except Exception as e:
        print(f"An error occurred: {e}")


if __name__ == "__main__":
    main()
