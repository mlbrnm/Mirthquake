import tkinter as tk
from tkinter import ttk, messagebox, font, scrolledtext
import time
import threading
from hl7 import Connection, Message
from datetime import datetime

class MirthLoadTester:
    def __init__(self, root):
        self.root = root
        self.root.title("Mirth Load Tester")
        self.root.geometry("600x700")
        
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
        header.grid(row=0, column=0, columnspan=2, pady=(0, 30))
        
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
        button_frame.grid(row=4, column=0, columnspan=2, pady=20)
        
        # Control buttons
        self.start_button = ttk.Button(button_frame, text="Start", command=self.start_test, width=15)
        self.start_button.grid(row=0, column=0, padx=10)
        
        self.stop_button = ttk.Button(button_frame, text="Stop", command=self.stop_test, state=tk.DISABLED, width=15)
        self.stop_button.grid(row=0, column=1, padx=10)
        
        # Clear log button
        self.clear_button = ttk.Button(button_frame, text="Clear Log", command=self.clear_log, width=15)
        self.clear_button.grid(row=0, column=2, padx=10)
        
        # Debug Log
        log_label = ttk.Label(self.main_frame, text="Debug Log:", font=status_font)
        log_label.grid(row=5, column=0, columnspan=2, pady=(20,5), sticky=tk.W)
        
        self.debug_log = scrolledtext.ScrolledText(self.main_frame, height=15, width=70)
        self.debug_log.grid(row=6, column=0, columnspan=2, sticky=(tk.W, tk.E))
        
        # Testing state
        self.is_running = False
        self.messages_sent = 0
        
        # Sample HL7 message (placeholder)
        self.sample_message = Message()
        self.sample_message.SetText('<Rec CRC="D90F"><Ver>1</Ver><DtTm>2024-12-04T10:25:20</DtTm><Type>1</Type><Prm><Id>TSSRNB</Id><Val>XXXX1234</Val></Prm><Prm><Id>ADPKID</Id><Val>003083265144</Val></Prm><Prm><Id>DMBLKS</Id><Val>1</Val></Prm><Prm><Id>DMBLDN</Id><Val>5008</Val></Prm><Prm><Id>DMBLBP</Id><Val>NARP</Val></Prm><Prm><Id>DMGRID</Id><Val>dia</Val></Prm><Prm><Id>DCUFVO</Id><Val>1036</Val></Prm><Prm><Id>DCUFTM</Id><Val>115</Val></Prm><Prm><Id>DCUFRA</Id><Val>500</Val></Prm><Prm><Id>DCISVA</Id><Val>0</Val></Prm><Prm><Id>DCRIUT</Id><Val>0</Val></Prm><Prm><Id>DCTHTM</Id><Val>7351</Val></Prm><Prm><Id>DCNACV</Id><Val>1370</Val></Prm><Prm><Id>DCARPR</Id><Val>-10</Val></Prm><Prm><Id>DCVEPR</Id><Val>50</Val></Prm><Prm><Id>DCTMPV</Id><Val>25</Val></Prm><Prm><Id>DCBLFL</Id><Val>400</Val></Prm><Prm><Id>DCPRBV</Id><Val>4880</Val></Prm><Prm><Id>DCRIVO</Id><Val>0</Val></Prm><Prm><Id>DCDITP</Id><Val>359</Val></Prm><Prm><Id>DCDIFL</Id><Val>493</Val></Prm><Prm><Id>DCENDC</Id><Val>137</Val></Prm><Prm><Id>DCTHBO</Id><Val>0</Val></Prm><Prm><Id>DCHEPR</Id><Val>0</Val></Prm><Prm><Id>DCHEPV</Id><Val>0</Val></Prm><Prm><Id>DCHDFT</Id><Val>0</Val></Prm><Prm><Id>DCHDFV</Id><Val>0</Val></Prm><Prm><Id>DCHDFC</Id><Val>0</Val></Prm><Prm><Id>DCHDRA</Id><Val>0</Val></Prm><Prm><Id>DCBFSP</Id><Val></Val></Prm><Prm><Id>DCCLMR</Id><Val>258</Val></Prm><Prm><Id>DCEFKT</Id><Val>311</Val></Prm><Prm><Id>DCEKTV</Id><Val>0</Val></Prm><Prm><Id>DCPSPK</Id><Val>0</Val></Prm><Prm><Id>DCPLNA</Id><Val>1411</Val></Prm><Prm><Id>DCPPNA</Id><Val></Val></Prm><Prm><Id>DCNARM</Id><Val></Val></Prm><Prm><Id>DCNARD</Id><Val></Val></Prm><Prm><Id>DCNAZM</Id><Val></Val></Prm><Prm><Id>DCSHFL</Id><Val></Val></Prm><Prm><Id>DCAFTP</Id><Val>3534</Val></Prm><Prm><Id>DCVFTP</Id><Val>3530</Val></Prm><Prm><Id>DCBDTD</Id><Val>-35</Val></Prm><Prm><Id>DCMERE</Id><Val>5411</Val></Prm><Prm><Id>DCOMBT</Id><Val>0</Val></Prm><Prm><Id>DCROST</Id><Val>0</Val></Prm><Prm><Id>DCHMGL</Id><Val></Val></Prm><Prm><Id>DCHMCT</Id><Val></Val></Prm><Prm><Id>DCTPCO</Id><Val></Val></Prm><Prm><Id>DCRBVO</Id><Val></Val></Prm><Prm><Id>DCMRBV</Id><Val></Val></Prm><Prm><Id>DCKKTM</Id><Val></Val></Prm><Prm><Id>DCHCTS</Id><Val></Val></Prm><Prm><Id>DCHCTE</Id><Val></Val></Prm><Prm><Id>DCHBGS</Id><Val></Val></Prm><Prm><Id>DCHBGE</Id><Val></Val></Prm><Prm><Id>DCTPCS</Id><Val></Val></Prm><Prm><Id>DCTPCE</Id><Val></Val></Prm><Prm><Id>DCXPOV</Id><Val></Val></Prm><Prm><Id>DCKTVT</Id><Val>0</Val></Prm><Prm><Id>DCXPOR</Id><Val></Val></Prm><Prm><Id>DCXESV</Id><Val>0</Val></Prm><Prm><Id>DCDATP</Id><Val>360</Val></Prm><Prm><Id>DCDAFL</Id><Val>500</Val></Prm><Prm><Id>DCRITT</Id><Val>0</Val></Prm><Prm><Id>DCUFRV</Id><Val>500</Val></Prm><Prm><Id>DCUFIV</Id><Val>0</Val></Prm><Prm><Id>DSNAGA</Id><Val>137</Val></Prm><Prm><Id>DSNAAB</Id><Val>350</Val></Prm><Prm><Id>DCHEPA</Id><Val></Val></Prm><Prm><Id>DCCLMA</Id><Val>249</Val></Prm><Prm><Id>DCHDAR</Id><Val></Val></Prm><Prm><Id>DCSNSA</Id><Val></Val></Prm><Prm><Id>DCTVBF</Id><Val>399</Val></Prm><Prm><Id>DCHDTM</Id><Val></Val></Prm><Prm><Id>DCHDFP</Id><Val></Val></Prm><Prm><Id>DCHDFO</Id><Val></Val></Prm><Prm><Id>DCHFPT</Id><Val></Val></Prm><Prm><Id>DCHFPO</Id><Val></Val></Prm><Prm><Id>DCLTDF</Id><Val>20241203081300</Val></Prm><Prm><Id>DCLTTM</Id><Val>20241201170100</Val></Prm><Prm><Id>DCRDST</Id><Val>2</Val></Prm><Prm><Id>DSUFVO</Id><Val>2000</Val></Prm><Prm><Id>DSUFTS</Id><Val>14400</Val></Prm><Prm><Id>DSUFRA</Id><Val>500</Val></Prm><Prm><Id>DSRMAX</Id><Val>3000</Val></Prm><Prm><Id>DSUFPT</Id><Val>0</Val></Prm><Prm><Id>DSDRYW</Id><Val>100</Val></Prm><Prm><Id>DSNPTP</Id><Val>0</Val></Prm><Prm><Id>DSNAST</Id><Val></Val></Prm><Prm><Id>DSNAGO</Id><Val>1370</Val></Prm><Prm><Id>DSNABI</Id><Val>350</Val></Prm><Prm><Id>DSCONC</Id><Val>A1245</Val></Prm><Prm><Id>DSCOSY</Id><Val>1</Val></Prm><Prm><Id>DSDLFW</Id><Val>500</Val></Prm><Prm><Id>DSDITP</Id><Val>360</Val></Prm><Prm><Id>DSDFAE</Id><Val>0</Val></Prm><Prm><Id>DSDFRT</Id><Val>15</Val></Prm><Prm><Id>DSHDFT</Id><Val>0</Val></Prm><Prm><Id>DSHRFM</Id><Val></Val></Prm><Prm><Id>DSHFEN</Id><Val>0</Val></Prm><Prm><Id>DSOBOV</Id><Val>120</Val></Prm><Prm><Id>DSHBFM</Id><Val>120</Val></Prm><Prm><Id>DSHBFA</Id><Val>0</Val></Prm><Prm><Id>DSHBEN</Id><Val>0</Val></Prm><Prm><Id>DSHFAE</Id><Val>1</Val></Prm><Prm><Id>DSHDFV</Id><Val></Val></Prm><Prm><Id>DSFITY</Id><Val>FX800 HDF</Val></Prm><Prm><Id>DSTPCO</Id><Val>69</Val></Prm><Prm><Id>DSSNSV</Id><Val>35</Val></Prm><Prm><Id>DSAUSN</Id><Val>1</Val></Prm><Prm><Id>DSSNRL</Id><Val>20</Val></Prm><Prm><Id>DSKKXP</Id><Val>400</Val></Prm><Prm><Id>DSKKIP</Id><Val>50</Val></Prm><Prm><Id>DSHMTC</Id><Val>35</Val></Prm><Prm><Id>DSTKTV</Id><Val>130</Val></Prm><Prm><Id>DSVURE</Id><Val>0</Val></Prm><Prm><Id>DSMIOC</Id><Val>1500</Val></Prm><Prm><Id>DSIOOC</Id><Val>1</Val></Prm><Prm><Id>DSHEPE</Id><Val>0</Val></Prm><Prm><Id>DSHEST</Id><Val>1800</Val></Prm><Prm><Id>DSHEPR</Id><Val>10</Val></Prm><Prm><Id>DSHBVO</Id><Val>10</Val></Prm><Prm><Id>DSHEAB</Id><Val>0</Val></Prm><Prm><Id>DSCRBV</Id><Val>95</Val></Prm><Prm><Id>DSMUBC</Id><Val>2800</Val></Prm><Prm><Id>DSBVMP</Id><Val>0</Val></Prm><Prm><Id>DSBVMN</Id><Val>0</Val></Prm><Prm><Id>DSBVMR</Id><Val>0</Val></Prm><Prm><Id>DSBVMA</Id><Val>0</Val></Prm><Prm><Id>DSTDST</Id><Val>0</Val></Prm><Prm><Id>DSXSYP</Id><Val>165</Val></Prm><Prm><Id>DSISYP</Id><Val>90</Val></Prm><Prm><Id>DSXDIP</Id><Val>100</Val></Prm><Prm><Id>DSIDIP</Id><Val>50</Val></Prm><Prm><Id>DSXMUL</Id><Val>120</Val></Prm><Prm><Id>DSIMUL</Id><Val>70</Val></Prm><Prm><Id>DSXPUL</Id><Val>150</Val></Prm><Prm><Id>DSIPUL</Id><Val>40</Val></Prm><Prm><Id>DSCYTM</Id><Val>0</Val></Prm><Prm><Id>DSBPPC</Id><Val>196</Val></Prm><Prm><Id>DSNACT</Id><Val>0</Val></Prm><Prm><Id>DSNADZ</Id><Val>0</Val></Prm><Prm><Id>DSNADC</Id><Val>0</Val></Prm><Prm><Id>DSPLKA</Id><Val>48</Val></Prm><Prm><Id>TSDVNM</Id><Val>5008</Val></Prm><Prm><Id>TSDMNF</Id><Val>Fresenius Medical Care</Val></Prm><Prm><Id>TSDLBL</Id><Val>NARP</Val></Prm><Prm><Id>TSSRNB</Id><Val>0VEANW70</Val></Prm><Prm><Id>TSSSWV</Id><Val>462</Val></Prm><Prm><Id>TSMAIN</Id><Val>2</Val></Prm><Prm><Id>TSALST</Id><Val>0</Val></Prm><Prm><Id>TSBCSB</Id><Val>767F</Val></Prm><Prm><Id>TSBCAB</Id><Val>FFFF</Val></Prm><Prm><Id>TSDLFT</Id><Val>1</Val></Prm><Prm><Id>TSTSTR</Id><Val>20241204082000</Val></Prm><Prm><Id>TSPRGR</Id><Val>51</Val></Prm><Prm><Id>TSMOPT</Id><Val>1021</Val></Prm></Rec>')
        
        # Initial log message
        self.log_message("Load Tester initialized")
        self.log_message("Ready to start testing")
        
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
