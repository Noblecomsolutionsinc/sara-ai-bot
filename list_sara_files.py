import os

def list_files_recursively(path, indent=0):
    lines = []
    for entry in os.listdir(path):
        full_path = os.path.join(path, entry)
        lines.append("  " * indent + entry)
        if os.path.isdir(full_path):
            lines.extend(list_files_recursively(full_path, indent + 1))
    return lines

current_dir = os.getcwd()
lines = [f"Listing all files and folders in: {current_dir}\n"]
lines.extend(list_files_recursively(current_dir))

# Save output to file
with open("sara_file_list.txt", "w") as f:
    f.write("\n".join(lines))

print("File structure saved to sara_file_list.txt")
