"""
DEXIS X-Ray Monitor
Monitors the DEXIS Imaging Suite folder for new x-ray images and sends notifications.
"""

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from plyer import notification

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('xray_monitor.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


class XRayFileHandler(FileSystemEventHandler):
    """Handles file system events for DEXIS x-ray files."""
    
    def __init__(self, config):
        self.config = config
        self.processed_files = set()
        logger.info("X-Ray File Handler initialized")
    
    def on_created(self, event):
        """Called when a new file is created."""
        if event.is_directory:
            return
        
        file_path = Path(event.src_path)
        
        # Process .dex files (x-rays) and .png files (intraoral/extraoral images)
        if file_path.suffix.lower() in ['.dex', '.png', '.jpg', '.jpeg']:
            self.handle_new_xray(file_path)
    
    def on_modified(self, event):
        """Called when a file is modified."""
        if event.is_directory:
            return
        
        file_path = Path(event.src_path)
        
        # Process .dex files (x-rays) and .png files (intraoral/extraoral images)
        if file_path.suffix.lower() in ['.dex', '.png', '.jpg', '.jpeg']:
            # Check if we've already processed this file
            if str(file_path) not in self.processed_files:
                self.handle_new_xray(file_path)
    
    def handle_new_xray(self, file_path):
        """Process a new x-ray or intraoral image file and send notifications."""
        try:
            # Avoid duplicate processing
            if str(file_path) in self.processed_files:
                return
            
            self.processed_files.add(str(file_path))
            
            # Wait a moment for file to be fully written
            import time
            time.sleep(0.5)
            
            # Check if file still exists and has content
            if not file_path.exists():
                return
            
            # Get file info
            file_size = file_path.stat().st_size
            if file_size < 100:  # Skip very small files (might be incomplete)
                return
            
            file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
            
            # Determine image type from file extension
            image_type = "X-Ray"
            if file_path.suffix.lower() in ['.png', '.jpg', '.jpeg']:
                image_type = "Intraoral/Extraoral Image"
            
            logger.info(f"New {image_type.lower()} detected: {file_path.name} ({file_size} bytes) at {file_time}")
            
            # Query database for patient info
            patient_info = self.get_patient_info(file_path)
            
            # Send notifications
            self.send_notifications(file_path, file_time, patient_info, image_type)
            
        except Exception as e:
            logger.error(f"Error processing image file {file_path}: {e}")
    
    def get_patient_info(self, file_path):
        """Query DEXIS database for patient information."""
        try:
            from dexis_db_query import DEXISDatabase
            
            # Load config if available
            config = None
            try:
                import json
                with open('config.json', 'r') as f:
                    config = json.load(f)
            except FileNotFoundError:
                pass
            
            file_id = file_path.stem  # Get filename without extension
            logger.info(f"Querying database for file ID: {file_id}")
            
            db = DEXISDatabase(config=config)
            try:
                if db.connect():
                    result = db.get_xray_info(file_id)
                    if result:
                        logger.info(f"Found patient info: Patient={result.get('PatientName', 'N/A')}, Type={result.get('XRayType', 'N/A')}")
                    return result
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Could not query database for patient info: {e}")
            return None
    
    def send_notifications(self, file_path, file_time, patient_info=None, image_type="X-Ray"):
        """Send all configured notifications."""
        # Build message with patient info if available
        message = f"New {image_type.lower()} taken: {file_path.name}\nTime: {file_time.strftime('%Y-%m-%d %H:%M:%S')}"
        
        if patient_info:
            # Extract patient name
            patient_name = None
            if 'PatientName' in patient_info and patient_info['PatientName']:
                patient_name = patient_info['PatientName']
            elif 'FirstName' in patient_info and 'LastName' in patient_info:
                if patient_info['FirstName'] and patient_info['LastName']:
                    patient_name = f"{patient_info['FirstName']} {patient_info['LastName']}"
            
            # Extract x-ray type
            xray_type = None
            if 'XRayType' in patient_info and patient_info['XRayType']:
                xray_type = patient_info['XRayType']
            elif 'ImageType' in patient_info and patient_info['ImageType']:
                xray_type = patient_info['ImageType']
            elif 'ImageDescription' in patient_info and patient_info['ImageDescription']:
                xray_type = patient_info['ImageDescription']
            
            # Extract tooth number (convert from FDI to Universal)
            # Note: Panoramic x-rays show all teeth, so don't display tooth numbers
            tooth_number = None
            is_panoramic = xray_type and 'Panoramic' in xray_type
            if not is_panoramic and 'Teeth' in patient_info and patient_info['Teeth']:
                from tooth_number_converter import format_teeth_for_display
                tooth_number = format_teeth_for_display(patient_info['Teeth'])
            
            if patient_name:
                message += f"\nPatient: {patient_name}"
            if xray_type:
                message += f"\nType: {xray_type}"
            if tooth_number:
                message += f"\n{tooth_number}"
            elif is_panoramic:
                message += f"\n(Shows all teeth)"
        
        # Desktop notification
        if self.config.get('notifications', {}).get('desktop', True):
            try:
                notification.notify(
                    title=f'DEXIS {image_type} Detected',
                    message=message,
                    timeout=10,
                    app_name='DEXIS Monitor'
                )
                logger.info("Desktop notification sent")
            except Exception as e:
                logger.error(f"Error sending desktop notification: {e}")
        
        # Email notification (if configured)
        if self.config.get('notifications', {}).get('email', False):
            self.send_email_notification(file_path, file_time)
    
    def send_email_notification(self, file_path, file_time):
        """Send email notification (if configured)."""
        try:
            email_config = self.config.get('email', {})
            if not email_config.get('smtp_server'):
                logger.warning("Email notification enabled but email configuration is missing")
                return
            
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            
            msg = MIMEMultipart()
            msg['From'] = email_config.get('from_email', '')
            msg['To'] = email_config.get('to_email', '')
            msg['Subject'] = 'DEXIS X-Ray Detected'
            
            body = f"""
New x-ray detected in DEXIS:
- File: {file_path.name}
- Path: {file_path}
- Time: {file_time.strftime('%Y-%m-%d %H:%M:%S')}
- Size: {file_path.stat().st_size} bytes
"""
            msg.attach(MIMEText(body, 'plain'))
            
            server = smtplib.SMTP(email_config.get('smtp_server'), email_config.get('smtp_port', 587))
            server.starttls()
            server.login(email_config.get('smtp_username', ''), email_config.get('smtp_password', ''))
            server.send_message(msg)
            server.quit()
            
            logger.info("Email notification sent")
        except Exception as e:
            logger.error(f"Error sending email notification: {e}")


class DEXISMonitor:
    """Main monitor class for DEXIS x-ray detection."""
    
    def __init__(self, config_path='config.json'):
        self.config = self.load_config(config_path)
        self.observer = None
        self.event_handler = None
        
    def load_config(self, config_path):
        """Load configuration from JSON file."""
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            logger.info(f"Configuration loaded from {config_path}")
            return config
        except FileNotFoundError:
            logger.error(f"Configuration file {config_path} not found. Using defaults.")
            return self.get_default_config()
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing configuration file: {e}")
            return self.get_default_config()
    
    def get_default_config(self):
        """Return default configuration."""
        return {
            "dexis_images_path": "\\\\YOUR_FILE_SERVER\\DEXIS Imaging Suite\\Data\\Images",
            "notifications": {
                "desktop": True,
                "email": False,
                "log_file": True
            }
        }
    
    def start(self):
        """Start monitoring the DEXIS Images and Thumbnails folders."""
        images_path = self.config.get('dexis_images_path')
        
        if not images_path:
            logger.error("DEXIS Images path not configured!")
            return False
        
        # Check if path exists
        if not os.path.exists(images_path):
            logger.error(f"DEXIS Images path does not exist: {images_path}")
            logger.info("Please check the path in config.json and ensure you have network access to the file server")
            return False
        
        # Also watch Thumbnails folder for intraoral photos
        base_path = os.path.dirname(os.path.dirname(images_path))  # Go up to Data folder
        thumbnails_path = os.path.join(base_path, "Thumbnails")
        
        logger.info(f"Starting monitor for: {images_path}")
        if os.path.exists(thumbnails_path):
            logger.info(f"Also monitoring: {thumbnails_path}")
        
        # Create event handler
        self.event_handler = XRayFileHandler(self.config)
        
        # Create observer
        self.observer = Observer()
        
        # Watch Images folder
        self.observer.schedule(
            self.event_handler,
            images_path,
            recursive=True  # Monitor all subdirectories
        )
        
        # Also watch Thumbnails folder if it exists
        if os.path.exists(thumbnails_path):
            self.observer.schedule(
                self.event_handler,
                thumbnails_path,
                recursive=True
            )
        
        # Start observer
        self.observer.start()
        logger.info("Monitor started. Waiting for new x-rays and intraoral photos...")
        
        return True
    
    def stop(self):
        """Stop monitoring."""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            logger.info("Monitor stopped")
    
    def run(self):
        """Run the monitor continuously."""
        if not self.start():
            return
        
        try:
            # Keep the script running
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        finally:
            self.stop()


def main():
    """Main entry point."""
    print("=" * 60)
    print("DEXIS X-Ray Monitor")
    print("=" * 60)
    print()
    
    monitor = DEXISMonitor()
    monitor.run()


if __name__ == '__main__':
    main()

