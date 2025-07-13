import os
import re

# Define the file path
file_path = "/home/perfect/InsipiraHub/backup.py"

# Check if the file exists
if not os.path.exists(file_path):
    print(f"Error: The file {file_path} does not exist.")
    exit(1)

# Read the content of the file
try:
    with open(file_path, "r") as file:
        content = file.read()
except Exception as e:
    print(f"Error reading the file: {e}")
    exit(1)

# Perform case-insensitive replacement of "dashboard" with "view_posts"
# Use re.sub with re.IGNORECASE to match any case variation of "dashboard"
# \b ensures we match "dashboard" as a whole word (not part of another word)
updated_content = re.sub(r'\bdashboard\b', 'view_posts', content, flags=re.IGNORECASE)

# Write the updated content back to the file
try:
    with open(file_path, "w") as file:
        file.write(updated_content)
    print(f"Successfully replaced all case variations of 'dashboard' with 'view_posts' in {file_path}")
except Exception as e:
    print(f"Error writing to the file: {e}")
    exit(1)
