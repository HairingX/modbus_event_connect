"""Update the file version."""
import os
import sys

def update__version__():
    """Update the file version."""
    version = "0.0.0"
    file_path = False

    for index, value in enumerate(sys.argv):
        if value in ["--version", "-V"]:
            version = str(sys.argv[index + 1]).replace("v", "")
        if value in ["--file", "-P"]:
            file_path = str(sys.argv[index + 1]).strip('/ ')

    if not file_path:
        sys.exit("Missing file")

    full_file_path = f"{os.getcwd()}/{file_path}"
    # Step 1: Open the file in read mode and read all lines
    with open(full_file_path, "r", encoding="UTF-8") as file:
        lines = file.readlines()

    # Step 2: Modify the specific line
    for i, line in enumerate(lines):
        if "__version__" in line:
            old_version = line.split('"')[1]
            lines[i] = line.replace(old_version, version)
            break

    # Step 3: Open the file in write mode and write all lines back to the file
    with open(full_file_path, "w", encoding="UTF-8") as file:
        file.writelines(lines)


update__version__()
