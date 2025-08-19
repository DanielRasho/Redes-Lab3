import logging

class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

# Custom colored formatter
class ColoredFormatter(logging.Formatter):
    COLOR_MAP = {
        'RECEIVED': Colors.GREEN,
        'SENT': Colors.BLUE,
        'FORWARDED': Colors.CYAN,
        'FLOODED': Colors.MAGENTA,
        'ERROR': Colors.RED,
        'WARNING': Colors.YELLOW,
        'INFO': Colors.WHITE
    }
    
    def format(self, record):
        log_message = super().format(record)
        for action, color in self.COLOR_MAP.items():
            if f'[{action}]' in log_message:
                return f"{color}{log_message}{Colors.ENDC}"
        return log_message

# Configure colored logging
def setup_colored_logging():
    logger = logging.getLogger()
    handler = logging.StreamHandler()
    formatter = ColoredFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)