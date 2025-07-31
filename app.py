import os
import subprocess
import shlex
import json
from flask import Flask, render_template, request, jsonify, send_file
import threading
import queue
import time
import uuid # For unique filenames
import shutil # Added for shutil.which
import sys # To detect OS and get command-line arguments

app = Flask(__name__)

# Directory to store temporary files (e.g., uploaded target files, scan outputs)
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# In-memory storage for exiftool outputs (for demonstration).
# In a real-world app, consider a more persistent and scalable solution (e.g., database, cloud storage).
exiftool_outputs = {}
exiftool_processes = {} # To keep track of running exiftool processes
exiftool_queues = {} # To store queues for real-time output

# Load examples from exiftool_examples.txt
def load_examples(filename="exiftool_examples.txt"):
    """Loads examples from a JSON file."""
    try:
        # Assuming exiftool_examples.txt is in the same directory as app.py
        filepath = os.path.join(os.path.dirname(__file__), filename)
        with open(filepath, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: Examples file '{filename}' not found. Please ensure it's in the same directory as app.py.")
        return []
    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from '{filename}'. Please check file format.")
        return []

@app.route('/')
def index():
    """Renders the main HTML page for the Exiftool GUI."""
    return render_template('index.html')

@app.route('/run_exiftool', methods=['POST'])
def run_exiftool():
    """
    Endpoint to run exiftool commands.
    Supports file uploads and real-time output streaming.
    """
    data = request.form
    target_file_id = data.get('target_file_id') # ID of the uploaded file
    target_path = data.get('target_path', '').strip() # User-provided path/URL
    exiftool_command_str = data.get('exiftool_command', '').strip()
    output_filename = data.get('output_filename', '').strip()

    if not exiftool_command_str and not target_file_id and not target_path:
        return jsonify({'status': 'error', 'message': 'No command, target file, or target path provided.'}), 400

    # Determine the actual target for exiftool
    actual_target = None
    if target_file_id:
        # If a file was uploaded, use its path
        uploaded_file_path = os.path.join(UPLOAD_FOLDER, target_file_id)
        if not os.path.exists(uploaded_file_path):
            return jsonify({'status': 'error', 'message': f'Uploaded file not found: {target_file_id}'}), 404
        actual_target = uploaded_file_path
    elif target_path:
        # Use the user-provided path/URL
        actual_target = target_path

    if not actual_target and not exiftool_command_str:
        return jsonify({'status': 'error', 'message': 'No valid target or command to run exiftool.'}), 400

    # Prepend 'exiftool' to the command string if not already present, and add target
    full_command = []
    if not exiftool_command_str.startswith('exiftool'):
        full_command.append('exiftool')

    # Add command options from the GUI
    if exiftool_command_str:
        # Use shlex.split to correctly handle spaces in arguments (e.g., "tag='value with spaces'")
        full_command.extend(shlex.split(exiftool_command_str))

    # Add the target if it's not already part of the command string
    # This check is a bit simplistic; a more robust solution might parse the command
    # to see if a target is already explicitly included. For now, we assume if
    # exiftool_command_str doesn't contain a file/path, we add it.
    if actual_target and actual_target not in full_command:
        full_command.append(actual_target)


    process_id = str(uuid.uuid4())
    output_queue = queue.Queue()
    exiftool_queues[process_id] = output_queue

    def run_exiftool_process(command, pid, output_q, output_file):
        try:
            # Check if exiftool is installed
            if shutil.which("exiftool") is None:
                output_q.put("ERROR: exiftool is not installed or not in PATH. Please install it first.\n")
                output_q.put("---PROCESS_COMPLETE---")
                return

            # If an output file is specified, redirect exiftool's stdout to it
            stdout_redirect = None
            if output_file:
                # Ensure the output file is within the UPLOAD_FOLDER for security and management
                output_file_path = os.path.join(UPLOAD_FOLDER, output_file)
                # Ensure the directory exists
                os.makedirs(os.path.dirname(output_file_path), exist_ok=True)
                stdout_redirect = open(output_file_path, 'w')

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, # Redirect stderr to stdout
                text=True,
                bufsize=1, # Line-buffered
                universal_newlines=True # Decode output as text
            )
            exiftool_processes[pid] = process

            for line in process.stdout:
                output_q.put(line)
                if stdout_redirect:
                    stdout_redirect.write(line)

            process.wait() # Wait for the process to terminate
            if stdout_redirect:
                stdout_redirect.close()

            output_q.put(f"\nExiftool process finished with exit code {process.returncode}\n")
            exiftool_outputs[pid] = {'status': 'completed', 'return_code': process.returncode}

        except FileNotFoundError:
            output_q.put("ERROR: 'exiftool' command not found. Please ensure Exiftool is installed and in your system's PATH.\n")
            exiftool_outputs[pid] = {'status': 'error', 'message': 'Exiftool not found.'}
        except Exception as e:
            output_q.put(f"ERROR: An unexpected error occurred: {e}\n")
            exiftool_outputs[pid] = {'status': 'error', 'message': str(e)}
        finally:
            output_q.put("---PROCESS_COMPLETE---")
            if pid in exiftool_processes:
                del exiftool_processes[pid]
            if pid in exiftool_queues:
                del exiftool_queues[pid]


    # Start exiftool in a new thread
    thread = threading.Thread(
        target=run_exiftool_process,
        args=(full_command, process_id, output_queue, output_filename)
    )
    thread.daemon = True # Allow the thread to exit when the main program exits
    thread.start()

    return jsonify({'status': 'success', 'message': 'Exiftool command started.', 'process_id': process_id})

@app.route('/stream_output/<process_id>')
def stream_output(process_id):
    """Endpoint to stream real-time output from a running exiftool process."""
    if process_id not in exiftool_queues:
        return jsonify({'status': 'error', 'message': 'Process not found or already completed.'}), 404

    def generate():
        q = exiftool_queues[process_id]
        while True:
            try:
                line = q.get(timeout=1) # Wait for a line with a timeout
                if line == "---PROCESS_COMPLETE---":
                    break
                yield line
            except queue.Empty:
                # If queue is empty, check if process is still running
                if process_id not in exiftool_processes:
                    break # Process finished and queue is empty
                time.sleep(0.1) # Small delay before checking again
        # Ensure the process is cleaned up if it somehow wasn't
        if process_id in exiftool_processes:
            try:
                exiftool_processes[process_id].terminate()
            except Exception as e:
                print(f"Error terminating process {process_id}: {e}")
            del exiftool_processes[process_id]
        if process_id in exiftool_queues:
            del exiftool_queues[process_id]

    return app.response_class(generate(), mimetype='text/plain')

@app.route('/upload_file', methods=['POST'])
def upload_file():
    """Endpoint to handle file uploads for exiftool processing."""
    if 'target_file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file part'}), 400
    file = request.files['target_file']
    if file.filename == '':
        return jsonify({'status': 'error', 'message': 'No selected file'}), 400
    if file:
        file_id = str(uuid.uuid4()) + os.path.splitext(file.filename)[1]
        filepath = os.path.join(UPLOAD_FOLDER, file_id)
        file.save(filepath)
        return jsonify({'status': 'success', 'message': 'File uploaded successfully', 'file_id': file_id, 'filename': file.filename}), 200
    return jsonify({'status': 'error', 'message': 'File upload failed'}), 500

@app.route('/get_examples', methods=['GET'])
def get_examples():
    """Endpoint to retrieve exiftool examples."""
    examples = load_examples()
    return jsonify(examples)

@app.route('/install_exiftool', methods=['POST'])
def install_exiftool():
    """
    Endpoint to handle exiftool installation based on OS.
    Supports Linux (apt) and Termux (pkg) automatic installation.
    """
    os_type = request.json.get('os_type')
    
    if os_type == 'linux':
        if sys.platform.startswith('linux'):
            try:
                # Update package lists
                update_process = subprocess.run(['sudo', 'apt', 'update'], capture_output=True, text=True, check=True)
                print(update_process.stdout)
                print(update_process.stderr)

                # Install exiftool
                install_process = subprocess.run(['sudo', 'apt', 'install', '-y', 'exiftool'], capture_output=True, text=True, check=True)
                print(install_process.stdout)
                print(install_process.stderr)
                return jsonify({
                    'status': 'success',
                    'message': 'Exiftool installed successfully on Linux.',
                    'output': install_process.stdout + install_process.stderr
                }), 200
            except subprocess.CalledProcessError as e:
                return jsonify({
                    'status': 'error',
                    'message': f'Failed to install Exiftool on Linux: {e.stderr}',
                    'output': e.stdout + e.stderr
                }), 500
            except FileNotFoundError:
                return jsonify({
                    'status': 'error',
                    'message': 'apt command not found. Are you on a Debian/Ubuntu-based system?',
                    'output': 'apt command not found.'
                }), 500
        else:
            return jsonify({
                'status': 'error',
                'message': 'Exiftool installation via this interface is only supported on Linux systems.',
                'output': 'Operating system is not Linux.'
            }), 400
    
    elif os_type == 'termux':
        if os.environ.get('TERMUX_VERSION'): # Check for Termux environment variable
            try:
                # Update package lists
                update_process = subprocess.run(['pkg', 'update', '-y'], capture_output=True, text=True, check=True)
                print(update_process.stdout)
                print(update_process.stderr)

                # Install exiftool
                install_process = subprocess.run(['pkg', 'install', '-y', 'exiftool'], capture_output=True, text=True, check=True)
                print(install_process.stdout)
                print(install_process.stderr)
                return jsonify({
                    'status': 'success',
                    'message': 'Exiftool installed successfully on Termux.',
                    'output': install_process.stdout + install_process.stderr
                }), 200
            except subprocess.CalledProcessError as e:
                return jsonify({
                    'status': 'error',
                    'message': f'Failed to install Exiftool on Termux: {e.stderr}',
                    'output': e.stdout + e.stderr
                }), 500
            except FileNotFoundError:
                return jsonify({
                    'status': 'error',
                    'message': 'pkg command not found. Are you on Termux?',
                    'output': 'pkg command not found.'
                }), 500
        else:
            return jsonify({
                'status': 'error',
                'message': 'Exiftool installation via this interface is only supported on Termux systems.',
                'output': 'Operating system is not Termux.'
            }), 400
    
    elif os_type in ['windows', 'macos']:
        # For Windows and macOS, provide manual instructions
        return jsonify({
            'status': 'info',
            'message': 'Manual installation required.',
            'output': f'Please follow the manual installation steps for {os_type.capitalize()}.'
        }), 200
    
    else:
        return jsonify({
            'status': 'error',
            'message': 'Unsupported OS type for automatic installation.',
            'output': 'Please select a valid OS type (linux, termux, windows, macos).'
        }), 400

# Function to gracefully shut down the Flask server
def shutdown_server():
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        raise RuntimeError('Not running with the Werkzeug Server')
    func()

@app.route('/shutdown', methods=['POST'])
def shutdown():
    """Endpoint to gracefully shut down the Flask application."""
    print("Received shutdown request.")
    # You might want to add authentication/authorization here in a real app
    shutdown_server()
    return 'Server shutting down...', 200

if __name__ == '__main__':
    # Default port if no argument is provided (e.g., if run directly)
    port = 5000 # This will be overridden by the PHP dashboard

    # Check for a --port argument passed from the PHP dashboard
    if '--port' in sys.argv:
        try:
            # Get the index of '--port' and then the next argument (which is the port number)
            port_index = sys.argv.index('--port') + 1
            port = int(sys.argv[port_index])
        except (ValueError, IndexError):
            print("Warning: Invalid or missing port argument for sub-app. Using default port.")
    
    print(f"Exiftool sub-app is starting on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)

