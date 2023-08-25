import subprocess

def run_command_with_logs(cmd):
    # This function will start the command and print its output in real-time
    with subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        try:
            # Print the logs in real-time
            for line in proc.stdout:
                print(line, end='')

            # Wait until process finishes and get the exit code
            proc.wait()

        except KeyboardInterrupt:
            # Send Ctrl+C signal to the process
            proc.terminate()
            proc.wait()

def main():
    try:
        print("Starting the passthrough system...")
        run_command_with_logs(r'C:\Users\avide\Documents\Work\MLDSAI\winfsp\tst\passthrough-augment\build\Debug\passthrough-x64.exe -p C:\Users -m y:')  # Replace with the actual command or path to run passthrough
        
        print("\nPassthrough system stopped.")
        
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    main()
