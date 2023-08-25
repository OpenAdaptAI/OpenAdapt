import subprocess

def run_command(cmd):
    try:
        subprocess.check_call(cmd, shell=True)
    except subprocess.CalledProcessError as e:
        print(f"Command failed with error: {e}")

def main():
    try:
        print("Starting the passthrough system...")
        run_command('net use y: \\\\passthrough\\C$\\Users')
        
        # Keep the script running until Ctrl+C is pressed
        print("Passthrough system started. Press Ctrl+C to stop.")
        while True:
            pass
            
    except KeyboardInterrupt:
        print("\nCtrl+C detected. Deleting the passthrough system...")
        run_command('net use y: /delete')
        print("Passthrough system deleted. Exiting.")
        
if __name__ == "__main__":
    main()
