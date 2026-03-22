"""Convert tooth numbers between different numbering systems."""

def fdi_to_universal(fdi_number):
    """
    Convert FDI tooth number to Universal notation.
    
    FDI format: XY where X is quadrant (1-4), Y is tooth position (1-8)
    Universal format: 1-32 where 1-16 is upper, 17-32 is lower
    
    Quadrants:
    1 = Upper Right (UR)
    2 = Upper Left (UL)
    3 = Lower Left (LL)
    4 = Lower Right (LR)
    
    Tooth positions:
    1 = Central incisor
    2 = Lateral incisor
    3 = Canine
    4 = First premolar
    5 = Second premolar
    6 = First molar
    7 = Second molar
    8 = Third molar (wisdom tooth)
    """
    if not fdi_number or fdi_number == '':
        return None
    
    # Remove 'teeth=' prefix if present
    fdi_str = str(fdi_number).replace('teeth=', '').strip()
    
    # Handle multiple teeth separated by semicolons or other delimiters
    if ';' in fdi_str or '%3B' in fdi_str:
        # URL encoded semicolon is %3B
        teeth = fdi_str.replace('%3B', ';').split(';')
        converted = []
        for tooth in teeth:
            tooth = tooth.strip()
            if tooth:
                conv = fdi_to_universal_single(tooth)
                if conv:
                    converted.append(conv)
        return ', '.join(converted) if converted else None
    
    return fdi_to_universal_single(fdi_str)

def fdi_to_universal_single(fdi_str):
    """Convert a single FDI tooth number to Universal."""
    try:
        # Parse FDI number (e.g., "307" or "38")
        fdi_str = str(fdi_str).strip()
        if len(fdi_str) < 2:
            return None
        
        # Handle 3-digit format (e.g., 307 = quadrant 3, tooth 07)
        if len(fdi_str) == 3:
            quadrant = int(fdi_str[0])
            tooth_pos = int(fdi_str[1:3])
        elif len(fdi_str) == 2:
            quadrant = int(fdi_str[0])
            tooth_pos = int(fdi_str[1])
        else:
            return None
        
        # Validate quadrant and tooth position
        if quadrant < 1 or quadrant > 4:
            return None
        if tooth_pos < 1 or tooth_pos > 8:
            return None
        
        # Convert to Universal notation
        # Universal numbering:
        # Upper Right (1): 1-8 (1=central incisor, 8=third molar)
        # Upper Left (2): 9-16 (9=central incisor, 16=third molar) - REVERSED
        # Lower Left (3): 17-24 (17=third molar, 24=central incisor) - REVERSED
        # Lower Right (4): 25-32 (25=central incisor, 32=third molar)
        
        if quadrant == 1:  # Upper Right
            universal = tooth_pos
        elif quadrant == 2:  # Upper Left (reversed: 1->9, 2->10, ..., 8->16)
            universal = 8 + tooth_pos
        elif quadrant == 3:  # Lower Left (reversed: 8->17, 7->18, 6->19, ..., 1->24)
            universal = 25 - tooth_pos
        elif quadrant == 4:  # Lower Right (1->25, 2->26, ..., 8->32)
            universal = 24 + tooth_pos
        else:
            return None
        
        return f"#{universal}"
    
    except (ValueError, IndexError):
        return None

def format_teeth_for_display(teeth_field):
    """Format teeth field for display in notifications."""
    if not teeth_field:
        return None
    
    # Convert FDI to Universal
    universal = fdi_to_universal(teeth_field)
    
    if universal:
        return f"Tooth {universal}"
    
    # If conversion failed, return original
    return f"Teeth: {teeth_field}"

# Test the conversion
if __name__ == "__main__":
    test_cases = [
        "307",  # Lower left second molar -> should be #18
        "38",   # Lower left third molar -> should be #17
        "206%3B207%3B208%3B306%3B307%3B308",  # Multiple teeth
        "teeth=307",
    ]
    
    print("Testing tooth number conversion:")
    for test in test_cases:
        result = format_teeth_for_display(test)
        print(f"  {test} -> {result}")

