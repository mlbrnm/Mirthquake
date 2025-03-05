import tkinter as tk
from tkinter import ttk, messagebox, font, scrolledtext, filedialog
import time
import threading
from hl7 import Connection, Message
from datetime import datetime

class MirthLoadTester:
    def __init__(self, root):
        self.root = root
        self.root.title("Mirth Load Tester")
        self.root.geometry("600x600")
        
        # Configure grid weight for centering
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        
        # Create and set up the main frame
        self.main_frame = ttk.Frame(root, padding="20")
        self.main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # Configure main frame grid weights for centering
        self.main_frame.grid_columnconfigure(1, weight=1)
        for i in range(8):
            self.main_frame.grid_rowconfigure(i, weight=1)
            
        # Create header
        header_font = font.Font(family="Arial", size=24, weight="bold")
        header = ttk.Label(self.main_frame, text="MirthQuake Load Tester", font=header_font)
        header.grid(row=0, column=0, columnspan=2, pady=(0, 20))
        
        # Create a frame for input fields
        input_frame = ttk.Frame(self.main_frame)
        input_frame.grid(row=1, column=0, columnspan=2, sticky=(tk.E, tk.W))
        input_frame.grid_columnconfigure(1, weight=1)
        
        # IP Address input
        ttk.Label(input_frame, text="IP Address:").grid(row=0, column=0, sticky=tk.E, padx=(0,10), pady=5)
        self.ip_var = tk.StringVar(value="10.107.78.63")
        self.ip_entry = ttk.Entry(input_frame, textvariable=self.ip_var, width=30)
        self.ip_entry.grid(row=0, column=1, sticky=(tk.W, tk.E), pady=5)
        
        # Port input
        ttk.Label(input_frame, text="Port:").grid(row=1, column=0, sticky=tk.E, padx=(0,10), pady=5)
        self.port_var = tk.StringVar(value="2205")
        self.port_entry = ttk.Entry(input_frame, textvariable=self.port_var, width=30)
        self.port_entry.grid(row=1, column=1, sticky=(tk.W, tk.E), pady=5)
        
        # Messages per minute input
        ttk.Label(input_frame, text="Messages per minute:").grid(row=2, column=0, sticky=tk.E, padx=(0,10), pady=5)
        self.mpm_var = tk.StringVar(value="300")
        self.mpm_entry = ttk.Entry(input_frame, textvariable=self.mpm_var, width=30)
        self.mpm_entry.grid(row=2, column=1, sticky=(tk.W, tk.E), pady=5)
        
        # Status display
        status_font = font.Font(size=10)
        self.status_var = tk.StringVar(value="Ready")
        self.status_label = ttk.Label(self.main_frame, textvariable=self.status_var, font=status_font)
        self.status_label.grid(row=2, column=0, columnspan=2, pady=20)
        
        # Progress display
        self.progress_var = tk.StringVar(value="Messages sent: 0")
        self.progress_label = ttk.Label(self.main_frame, textvariable=self.progress_var, font=status_font)
        self.progress_label.grid(row=3, column=0, columnspan=2, pady=5)
        
        # Button frame for centered buttons
        button_frame = ttk.Frame(self.main_frame)
        button_frame.grid(row=4, column=0, columnspan=2, pady=10)
        
        # Control buttons
        self.start_button = ttk.Button(button_frame, text="Start Loop", command=self.start_test, width=15)
        self.start_button.grid(row=0, column=0, padx=10)
        
        self.stop_button = ttk.Button(button_frame, text="Stop", command=self.stop_test, state=tk.DISABLED, width=15)
        self.stop_button.grid(row=0, column=1, padx=10)
        
        # Single message button
        self.single_button = ttk.Button(button_frame, text="Send Once", command=self.send_single_message, width=15)
        self.single_button.grid(row=0, column=2, padx=10)
        
        # Clear log button
        self.clear_button = ttk.Button(button_frame, text="Clear Log", command=self.clear_log, width=15)
        self.clear_button.grid(row=0, column=3, padx=10)
        
        # Debug Log
        log_label = ttk.Label(self.main_frame, text="Debug Log:", font=status_font)
        log_label.grid(row=5, column=0, columnspan=2, pady=(20,5), sticky=tk.W)
        
        self.debug_log = scrolledtext.ScrolledText(self.main_frame, height=10, width=70)
        self.debug_log.grid(row=6, column=0, columnspan=2, sticky=(tk.W, tk.E))
        
        # Testing state
        self.is_running = False
        self.messages_sent = 0
        
        # Default message as fallback
        self.default_message = ''
        
        # Message file selection
        file_frame = ttk.Frame(self.main_frame)
        file_frame.grid(row=7, column=0, columnspan=2, pady=(10,5), sticky=(tk.W, tk.E))
        file_frame.grid_columnconfigure(1, weight=1)
        
        ttk.Label(file_frame, text="Message File:").grid(row=0, column=0, sticky=tk.E, padx=(0,10))
        self.file_path_var = tk.StringVar(value="message.xml")
        self.file_path_label = ttk.Label(file_frame, textvariable=self.file_path_var, wraplength=400)
        self.file_path_label.grid(row=0, column=1, sticky=tk.W)
        
        self.browse_button = ttk.Button(file_frame, text="Browse", command=self.browse_file)
        self.browse_button.grid(row=0, column=2, padx=(10,0))
        
        # Initialize message
        self.sample_message = Message()
        self.load_message()
        
        # Initial log message
        self.log_message("Load Tester initialized")
        self.log_message("Ready to start testing")
        
        # Schedule file picker to open after window appears
        self.root.after(100, self.browse_file)
        
    def browse_file(self):
        """Open file dialog to select message file"""
        file_path = filedialog.askopenfilename(
            title="Select Message File",
            filetypes=[("XML files", "*.xml"), ("All files", "*.*")]
        )
        if file_path:
            self.file_path_var.set(file_path)
            self.load_message()
    
    def load_message(self):
        """Load message from selected file or use default if file is invalid"""
        try:
            file_path = self.file_path_var.get()
            with open(file_path, 'r') as f:
                content = f.read().strip()
                if not content:
                    raise ValueError("Empty file")
                    
                # Basic XML validation - will raise if invalid
                import xml.etree.ElementTree as ET
                ET.fromstring(content)
                
                self.sample_message.SetText(content)
                self.log_message(f"Successfully loaded message from {file_path}")
                
        except Exception as e:
            self.log_message(f"Error loading {file_path} ({str(e)}), using default message", "WARNING")
            self.sample_message.SetText(self.default_message)
            
    def send_single_message(self):
        """Send a single message without starting the continuous loop"""
        if not self.validate_inputs():
            return
            
        # Disable buttons during send
        self.start_button.config(state=tk.DISABLED)
        self.single_button.config(state=tk.DISABLED)
        self.status_var.set("Sending single message...")
        
        # Start the send in a separate thread
        self.test_thread = threading.Thread(target=self.send_one_message)
        self.test_thread.daemon = True
        self.test_thread.start()
        
    def send_one_message(self):
        """Helper method to send one message and handle the response"""
        try:
            self.log_message(f"Attempting to connect to {self.ip_var.get()}:{self.port_var.get()}")
            conn = Connection(self.ip_var.get(), int(self.port_var.get()))
            
            try:
                conn.Open()
                self.log_message("Connection established successfully")
                
                # Log the message being sent (first few characters)
                msg_preview = str(self.sample_message.Render())[:50] + "..."
                self.log_message(f"Sending message: {msg_preview}")
                
                # Send message and get response
                response = conn.Send(self.sample_message)
                self.messages_sent += 1
                self.progress_var.set(f"Messages sent: {self.messages_sent}")
                
                # Log the response
                try:
                    response_text = response.decode('windows-1252')
                    self.log_message(f"Response received: {response_text}")
                except:
                    # If we can't decode the response, show the raw bytes
                    self.log_message(f"Raw response received: {response}")
                
                self.log_message("Closing connection...")
                conn.Close()
                self.log_message("Connection closed successfully")
                self.status_var.set("Single message sent successfully")
                
            except Exception as e:
                error_msg = f"Connection error: {str(e)}"
                self.log_message(error_msg, "ERROR")
                self.status_var.set(error_msg)
            
        except Exception as e:
            error_msg = f"Setup error: {str(e)}"
            self.log_message(error_msg, "ERROR")
            self.status_var.set(error_msg)
        
        # Re-enable buttons
        self.start_button.config(state=tk.NORMAL)
        self.single_button.config(state=tk.NORMAL)
        
    def log_message(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_entry = f"[{timestamp}] [{level}] {message}\n"
        self.debug_log.insert(tk.END, log_entry)
        self.debug_log.see(tk.END)
        
    def clear_log(self):
        self.debug_log.delete(1.0, tk.END)
        self.log_message("Log cleared")
        
    def validate_inputs(self):
        try:
            port = int(self.port_var.get())
            if port < 1 or port > 65535:
                raise ValueError("Port must be between 1 and 65535")
            
            mpm = int(self.mpm_var.get())
            if mpm < 1 or mpm > 1000:
                raise ValueError("Messages per minute must be between 1 and 1000")
                
            return True
        except ValueError as e:
            self.log_message(f"Validation error: {str(e)}", "ERROR")
            messagebox.showerror("Invalid Input", str(e))
            return False
    
    def start_test(self):
        if not self.validate_inputs():
            return
            
        self.is_running = True
        self.messages_sent = 0
        self.start_button.config(state=tk.DISABLED)
        self.single_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.status_var.set("Running...")
        
        self.log_message(f"Starting test with parameters:")
        self.log_message(f"IP: {self.ip_var.get()}")
        self.log_message(f"Port: {self.port_var.get()}")
        self.log_message(f"Messages per minute: {self.mpm_var.get()}")
        
        # Start the test in a separate thread
        self.test_thread = threading.Thread(target=self.run_test)
        self.test_thread.daemon = True
        self.test_thread.start()
    
    def stop_test(self):
        self.is_running = False
        self.start_button.config(state=tk.NORMAL)
        self.single_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.status_var.set("Stopped")
        self.log_message("Test stopped by user")
    
    def run_test(self):
        try:
            self.log_message(f"Attempting to connect to {self.ip_var.get()}:{self.port_var.get()}")
            conn = Connection(self.ip_var.get(), int(self.port_var.get()))
            
            try:
                conn.Open()
                self.log_message("Connection established successfully")
                
                mpm = int(self.mpm_var.get())
                delay = 60.0 / mpm  # Calculate delay between messages
                self.log_message(f"Calculated delay between messages: {delay:.3f} seconds")
                
                while self.is_running:
                    try:
                        # Log the message being sent (first few characters)
                        msg_preview = str(self.sample_message.Render())[:50] + "..."
                        self.log_message(f"Sending message: {msg_preview}")
                        
                        # Send message and get response
                        response = conn.Send(self.sample_message)
                        self.messages_sent += 1
                        self.progress_var.set(f"Messages sent: {self.messages_sent}")
                        
                        # Log the response
                        try:
                            response_text = response.decode('windows-1252')
                            self.log_message(f"Response received: {response_text}")
                        except:
                            # If we can't decode the response, show the raw bytes
                            self.log_message(f"Raw response received: {response}")
                            
                        if self.messages_sent % 10 == 0:  # Log every 10th message
                            self.log_message(f"Successfully sent {self.messages_sent} messages")
                            
                        time.sleep(delay)
                    except Exception as e:
                        error_msg = f"Error sending message: {str(e)}"
                        self.log_message(error_msg, "ERROR")
                        self.status_var.set(error_msg)
                        break
                
                self.log_message("Closing connection...")
                conn.Close()
                self.log_message("Connection closed successfully")
                
            except Exception as e:
                error_msg = f"Connection error: {str(e)}"
                self.log_message(error_msg, "ERROR")
                self.status_var.set(error_msg)
            
        except Exception as e:
            error_msg = f"Setup error: {str(e)}"
            self.log_message(error_msg, "ERROR")
            self.status_var.set(error_msg)
        
        if self.is_running:  # If we got here due to an error
            self.stop_test()

if __name__ == "__main__":
    root = tk.Tk()
    app = MirthLoadTester(root)
    root.mainloop()
