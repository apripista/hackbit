import os
import re
import shutil
from pathlib import Path

# Directory to search (current directory, InsipiraHub)
ROOT_DIR = Path.cwd()

# File extensions to process (add more if needed)
TEXT_FILE_EXTENSIONS = {'.py', '.html', '.css', '.js', '.txt', '.md', '.sql'}

# Directories to skip
SKIP_DIRS = {'.git', '__pycache__', 'venv', 'node_modules', 'backup'}

def backup_file(file_path):
    """Create a backup of the file before modifying or renaming."""
    backup_path = file_path.with_suffix(file_path.suffix + '.bak')
    shutil.copy2(file_path, backup_path)
    print(f"Created backup: {backup_path}")

def is_text_file(file_path):
    """Check if the file is a text file based on its extension."""
    return file_path.suffix.lower() in TEXT_FILE_EXTENSIONS

def replace_in_file_content(file_path):
    """Replace tfa (case-insensitive) with tfa in the given file's content."""
    try:
        # Read the file content
        with open(file_path, 'r', encoding='utf-8') as file:
            content = file.read()

        # Perform case-insensitive replacement
        new_content, count = re.subn(r'\b2[Ff][Aa]\b', 'tfa', content, flags=re.IGNORECASE)

        if count > 0:
            # Create a backup before modifying
            backup_file(file_path)
            # Write the modified content back to the file
            with open(file_path, 'w', encoding='utf-8') as file:
                file.write(new_content)
            print(f"Updated content in {file_path}: {count} replacements")
        else:
            print(f"No content changes in {file_path}")

        return count > 0

    except UnicodeDecodeError:
        print(f"Skipped {file_path}: Not a text file")
        return False
    except Exception as e:
        print(f"Error processing {file_path}: {e}")
        return False

def rename_file_if_needed(file_path):
    """Rename the file if its name contains 'tfa' (case-insensitive)."""
    file_name = file_path.name
    if re.search(r'2[Ff][Aa]', file_name, re.IGNORECASE):
        new_file_name = re.sub(r'2[Ff][Aa]', 'tfa', file_name, flags=re.IGNORECASE)
        new_file_path = file_path.parent / new_file_name

        # Create a backup of the original file
        backup_file(file_path)

        # Rename the file
        shutil.move(file_path, new_file_path)
        print(f"Renamed file: {file_path} -> {new_file_path}")
        return new_file_path
    return file_path

def update_references_in_files(old_name, new_name):
    """Update references to renamed files (e.g., in render_template calls)."""
    for root, dirs, files in os.walk(ROOT_DIR, topdown=True):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for file_name in files:
            file_path = Path(root) / file_name
            if is_text_file(file_path):
                try:
                    with open(file_path, 'r', encoding='utf-8') as file:
                        content = file.read()

                    # Replace references to the old filename
                    new_content, count = re.subn(
                        re.escape(old_name), new_name, content
                    )

                    if count > 0:
                        backup_file(file_path)
                        with open(file_path, 'w', encoding='utf-8') as file:
                            file.write(new_content)
                        print(f"Updated {count} references to {old_name} in {file_path}")
                except UnicodeDecodeError:
                    print(f"Skipped {file_path}: Not a text file")
                except Exception as e:
                    print(f"Error updating references in {file_path}: {e}")

def main():
    """Walk through the directory, replace content, and rename files."""
    print(f"Starting search and replace in {ROOT_DIR}")
    print("Replacing 'tfa' (case-insensitive) with 'tfa' in content and filenames...")

    # Collect files to process
    files_to_process = []
    for root, dirs, files in os.walk(ROOT_DIR, topdown=True):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for file_name in files:
            files_to_process.append(Path(root) / file_name)

    # Process files: rename first, then update content
    renamed_files = {}
    for file_path in files_to_process:
        if is_text_file(file_path):
            # Rename the file if needed
            new_file_path = rename_file_if_needed(file_path)
            if new_file_path != file_path:
                renamed_files[file_path.name] = new_file_path.name
            # Update content
            replace_in_file_content(new_file_path)

    # Update references to renamed files
    for old_name, new_name in renamed_files.items():
        update_references_in_files(old_name, new_name)

    print("Search and replace completed.")

if __name__ == "__main__":
    main()