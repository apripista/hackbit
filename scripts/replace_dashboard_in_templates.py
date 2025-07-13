import os
import re

# Define the directory path
directory_path = "/home/perfect/InsipiraHub/templates"

# Check if the directory exists
if not os.path.exists(directory_path):
    print(f"Error: The directory {directory_path} does not exist.")
    exit(1)

# Counter for modified files
modified_files = 0

# Iterate over all files in the directory
for root, dirs, files in os.walk(directory_path):
    for filename in files:
        file_path = os.path.join(root, filename)

        # Read the content of the file
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                content = file.read()
        except Exception as e:
            print(f"Error reading the file {file_path}: {e}")
            continue

        # Perform case-insensitive replacement of "dashboard" with "view_posts"
        # Remove \b to match "dashboard" even within strings like url_for('dashboard')
        updated_content = re.sub(r'dashboard', 'view_posts', content, flags=re.IGNORECASE)

        # Only write back if the content has changed
        if updated_content != content:
            try:
                with open(file_path, "w", encoding="utf-8") as file:
                    file.write(updated_content)
                print(f"Replaced 'dashboard' with 'view_posts' in {file_path}")
                modified_files += 1
            except Exception as e:
                print(f"Error writing to the file {file_path}: {e}")
                continue

# Summary
if modified_files == 0:
    print("No files were modified. No instances of 'dashboard' found.")
else:
    print(f"Finished! Modified {modified_files} files.")
