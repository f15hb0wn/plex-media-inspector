import csv
import tkinter as tk
from tkinter import ttk, Menu, simpledialog
import tkinter.messagebox as messagebox
import time
import yaml
import os
import subprocess
from plexapi.myplex import MyPlexAccount
from plexapi.exceptions import Unauthorized
from plexapi.server import PlexServer
import threading
import sqlite3
import hashlib

BITRATE_MINIMUM = 1000

# Open the database connection
conn = sqlite3.connect('database.db')

# Create a cursor object
cursor = conn.cursor()

# Create the file_hashes table if it doesn't exist
cursor.execute('''
    CREATE TABLE IF NOT EXISTS file_hashes (
        hash TEXT PRIMARY KEY,
        filename TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS media_resolutions (
        filename TEXT PRIMARY KEY,
        resolution INTEGER
    )
''')

# Commit the changes and close the connection
conn.commit()
conn.close()


def get_libraries():
    global plex_api
    return plex_api.library.sections()

# Width of the Screen
window_width = 1024
window_height = 780
total_width = window_width - 100

root = tk.Tk()
root.title("Fish's Plex Media Inspector")
root.geometry(str(window_width) + "x" + str(window_height))  

frame = tk.Frame(root)
frame.pack(fill=tk.BOTH, expand=1, padx=50, pady=50)

def get_library_item_count():
    global plex_api
    library = plex_api.library
    items = 0
    for item in library.all():
        if item.type in ['movie', 'show']:
            items += 1
    return items

scanning = False
scan_thread = None
current_title = ""

def start_scan():
    global library_total_item_count
    global library_items_scanned
    global per_item_scan_seconds
    global total_scan_seconds
    global plex_api
    global selected_library
    global scanning
    global scan_thread
    library_total_item_count = 0
    library_items_scanned = 0
    per_item_scan_seconds = 1
    total_scan_seconds = 0
    remaining_seconds = per_item_scan_seconds * library_total_item_count
    remaining_time = time.strftime('%H:%M:%S', time.gmtime(remaining_seconds))
    save_settings()
    update_progress()
    if not scanning:
        scanning = True
        library_menu.config(state="disabled")
        start_button.config(text="Abort Scan")
        library_total_item_count = get_library_item_count()
        update_progress()
        scan_thread = threading.Thread(target=scan_library_meta, args=(plex_api, selected_library.get()))
        scan_thread.start()
    else:
        scanning = False
        library_menu.config(state="enabled")
        start_button.config(text="Start Scan")
        if scan_thread is not None:
            scan_thread.join()  # Wait for the thread to finish
    
def scan_library_meta(plex, library_name):
    global library_items_scanned
    global total_scan_seconds
    global library_total_item_count
    global scanning
    global writer
    global current_title
    load_settings()
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()    
    # Create a variable for the CSV file
    csv_file = open('results.csv', 'w', newline='')

    # Create a writer object for the CSV file
    writer = csv.writer(csv_file)

    # Write the header row to the CSV file
    writer.writerow(['Library', 'Title', 'Plex Link', 'Disk Link', 'Problems'])

    # Get the library by name
    library = plex.library.section(library_name)

    # Create an empty list to store content with issues
    start_time = time.time()
    # Iterate over all items in the library
    for item in library.all():
        # Wait 1 second before scanning the next item
        time.sleep(1)
        title = item.title
        current_title = item.title
        # Check if scanning should be aborted
        if not scanning:
            conn.close()
            csv_file.close()
            break
        # Skip items that are not movies or shows
        if item.type not in ['movie', 'show']:
            total_scan_seconds = time.time() - start_time
            library_items_scanned += 1
            update_progress()
            continue
        if item.type == 'show':
            show_ts_problems = False
            episodes = []
            missing_credits = []
            seasons = []
            skip_show = False
            #Check for missing episodes in a season
            for episode in item.episodes():
                season = episode.seasonNumber
                if season not in seasons:
                    seasons.append(season)
                    episodes.append([])
                    missing_credits.append([])
                season_index = seasons.index(season)

                title = f"{item.title} S{str(episode.seasonNumber).zfill(2)}:E{str(episode.index).zfill(2)} : {episode.title}"
                episodes[season_index].append(episode.index)
                current_title = title
                update_progress()                
                if not hasattr(episode, 'hasCreditsMarker'):
                    total_scan_seconds = time.time() - start_time
                    update_progress()
                    continue
                if ignore_ts.get() and episode.media[0].parts[0].file.endswith('.ts'):
                    skip_show = True
                    break
                problems = []
                # Check if the item has detected credits
                if not episode.hasCreditsMarker and check_credits.get():
                    missing_credits[season_index].append(str(episode.index))
                # Check if the item has a valid bitrate
                bitrate = episode.media[0].bitrate
                if bitrate <= BITRATE_MINIMUM and validate_bitrate.get():
                    problems.append("Invalid bitrate")
                corrupt = False

                if check_encoding_errors.get() and not skip_show:
                    checksum = hashlib.md5(episode.media[0].parts[0].file.encode()).hexdigest()
                    cursor.execute('SELECT * FROM file_hashes WHERE hash = ?', (checksum,))
                    if cursor.fetchone() is None:
                        corrupt = check_corrupt_video(episode.media[0].parts[0].file)
                        if not corrupt:
                            cursor.execute('INSERT INTO file_hashes (hash, filename) VALUES (?, ?)', (checksum, episode.media[0].parts[0].file))
                            conn.commit()
                if corrupt:
                    problems.append("Corrupt video file detected")
                if no_ts.get() and episode.media[0].parts[0].file.endswith('.ts'):
                    show_ts_problems = True
                if problems:
                    add_result(library_name, title, episode.guid, episode.media[0].parts[0].file, problems)
                total_scan_seconds = time.time() - start_time
            library_items_scanned += 1
            # Check for show problems
            show_problems = []
            if show_ts_problems and not skip_show:
                show_problems.append('Show has episodes with .ts extension')
            # Evaluate each season
            if skip_show:
                continue
            for season in seasons:
                if season < 1:
                    continue
                season_index = seasons.index(season)
                season_number = season
                percent_credits = len(missing_credits[season_index]) / len(episodes[season_index])
                if percent_credits < 1 and percent_credits >= 0.5:
                    show_problems.append(f"Season {season_number} has missing credits for episodes {', '.join(missing_credits[season_index])}")
                last_episode = 1
                for episode in episodes[season_index]:
                    if not episode:
                        continue
                    if episode > 30:
                        break
                    if episode > last_episode:
                        last_episode = episode
                if last_episode == 1:
                    continue
                missing_episodes = []
                for i in range(1, last_episode):
                    if i not in episodes[season_index]:
                        missing_episodes.append(str(i))
                if missing_episodes:
                    show_problems.append(f"Missing episodes in season {season_number}: {', '.join(missing_episodes)}")
                    
            if show_problems:
                add_result(library_name, item.title, item.guid, '', show_problems)
            
            update_progress()
        else:
            update_progress()
            if not hasattr(item, 'hasCreditsMarker'):
                library_items_scanned += 1
                total_scan_seconds = time.time() - start_time
                update_progress()
                continue
            # Check if the file is marked as resolved in the database
            cursor.execute('SELECT resolution FROM media_resolutions WHERE filename = ?', (item.media[0].parts[0].file,))
            result = cursor.fetchone()
            if result is None:
                resolution = 0
                cursor.execute('INSERT INTO media_resolutions (filename, resolution) VALUES (?, ?)', (item.media[0].parts[0].file, resolution))
                conn.commit()
            else:
                resolution = result[0]
            if resolution == 1:
                library_items_scanned += 1
                total_scan_seconds = time.time() - start_time
                update_progress()
                continue
            problems = []
            # Check if the item has detected credits
            if not item.hasCreditsMarker and check_credits.get():
                problems.append("No credits detected")
            # Check if the item has a valid bitrate
            bitrate = item.media[0].bitrate
            if bitrate <= BITRATE_MINIMUM and validate_bitrate.get():
                problems.append("Invalid bitrate")
            corrupt = False
            if check_encoding_errors.get():
                checksum = hashlib.md5(item.media[0].parts[0].file.encode()).hexdigest()
                cursor.execute('SELECT * FROM file_hashes WHERE hash = ?', (checksum,))
                if cursor.fetchone() is None:
                    corrupt = check_corrupt_video(item.media[0].parts[0].file)
                    if not corrupt:
                        cursor.execute('INSERT INTO file_hashes (hash, filename) VALUES (?, ?)', (checksum, item.media[0].parts[0].file))
                        conn.commit()
            if corrupt:
                problems.append("Corrupt video file detected")
            if no_ts.get() and item.media[0].parts[0].file.endswith('.ts'):
                problems.append("File has .ts extension")
            if problems:
                add_result(library_name, title, item.guid, item.media[0].parts[0].file, problems)
            library_items_scanned += 1
            total_scan_seconds = time.time() - start_time
            update_progress()
    # Return the list of content without credits
    library_items_scanned = library_total_item_count
    total_scan_seconds = time.time() - start_time
    update_progress()
    csv_file.close()
    conn.close()

 
# Status variables
library_total_item_count = 0
library_items_scanned = 0
per_item_scan_seconds = 1
total_scan_seconds = 0
remaining_seconds = per_item_scan_seconds * library_total_item_count
remaining_time = time.strftime('%H:%M:%S', time.gmtime(remaining_seconds))
plex_connected = False
results = []

def add_result(library, title, plex_link, disk_link, problems):
    global writer
    # Determine the tag based on the number of existing children
    tag = "oddrow" if len(results_table.get_children()) % 2 == 0 else "evenrow"
    # Insert the result with the determined tag    
    results_table.insert('', 'end', values=(library, title, plex_link, disk_link, problems), tags=(tag))
    # Write the result to the CSV file
    writer.writerow([library, title, plex_link, disk_link, problems])


# Create variables for settings options and set them to True
check_credits = tk.BooleanVar(value=True)
validate_bitrate = tk.BooleanVar(value=True)
check_encoding_errors = tk.BooleanVar(value=True)
no_ts = tk.BooleanVar(value=True)
ignore_ts = tk.BooleanVar(value=False)
check_missing_episodes = tk.BooleanVar(value=True)

# Create variables for Plex settings
plex_account_name = tk.StringVar()
plex_password = tk.StringVar()
plex_token = tk.StringVar()
plex_server = tk.StringVar()
plex_server.set('http://127.0.0.1:32400')
plex_api = None


# Function to test the Plex connection
def test_plex_connection():
    global plex_connected
    global plex_api
    try:
        plex_api = PlexServer(plex_server.get(), plex_token.get())
        plex_connected = True
    except Unauthorized:
        plex_connected = False
        try:
            connection_status.config(text="Plex Connection: Unauthorized", fg="red")
            connection_indicator.itemconfig(connection_circle, fill="red")
            start_button.config(state=tk.DISABLED)
            library_menu.config(state="disabled")
            return False
        except Exception as e:
            return False
    if plex_connected:
        try:
            connection_status.config(text="Plex Connection: Connected", fg="green")
            connection_indicator.itemconfig(connection_circle, fill="green")
            library_menu.config(state="normal")
            return True
        except Exception as e:
            return True
# Function to update the progress bar and label
def update_progress():
    global library_total_item_count
    global per_item_scan_seconds
    global remaining_seconds
    global remaining_time
    global library_items_scanned
    global progress_label
    global progress
    global total_scan_seconds
    global current_title
    if library_total_item_count > 0:
        progress_value = library_items_scanned / library_total_item_count
    else:
        progress_value = 0
    if library_items_scanned > 0:
        per_item_scan_seconds = total_scan_seconds / library_items_scanned
    else:
        per_item_scan_seconds = 0
    remaining_seconds = per_item_scan_seconds * (library_total_item_count - library_items_scanned)
    remaining_time = time.strftime('%H:%M:%S', time.gmtime(remaining_seconds))
    progress_label.config(text="Progress: " + str(library_items_scanned) + " / " + str(library_total_item_count) + " items scanned. Estimated time remaining: " + remaining_time + ". \n" + current_title)
    progress['value'] = progress_value * 100
    root.update_idletasks()

def save_settings():
    settings = {
        'plex_account_name': plex_account_name.get(),
        'plex_token': plex_token.get(),
        'plex_server': plex_server.get(),
        'check_credits': check_credits.get(),
        'validate_bitrate': validate_bitrate.get(),
        'no_ts': no_ts.get(), # Added 'no_ts' to the settings dictionary
        'ignore_ts': ignore_ts.get(), # Added 'ignore_ts' to the settings dictionary
        'check_missing_episodes': check_missing_episodes.get(), # Added 'check_missing_episodes' to the settings dictionary
        'check_encoding_errors': check_encoding_errors.get(),
    }
    with open('settings.yaml', 'w') as file:
        yaml.dump(settings, file)


def load_settings():
    if not os.path.exists('settings.yaml'):
        save_settings()
    with open('settings.yaml', 'r') as file:
        settings = yaml.safe_load(file)
    plex_account_name.set(settings['plex_account_name'])
    plex_token.set(settings['plex_token'])
    plex_server.set(settings['plex_server'])
    check_credits.set(settings['check_credits'])
    validate_bitrate.set(settings['validate_bitrate'])
    no_ts.set(settings['no_ts']) # Added 'no_ts' to the settings dictionary
    ignore_ts.set(settings['ignore_ts']) # Added 'ignore_ts' to the settings dictionary
    check_missing_episodes.set(settings['check_missing_episodes']) # Added 'check_missing_episodes' to the settings dictionary
    check_encoding_errors.set(settings['check_encoding_errors'])


# Load settings when the program starts
load_settings()


# Create a frame for the connection indicator and status
connection_frame = tk.Frame(frame)
connection_frame.pack(pady=10)

# Create a canvas for the connection indicator inside the connection frame
connection_indicator = tk.Canvas(connection_frame, width=20, height=20)
connection_circle = connection_indicator.create_oval(5, 5, 15, 15, fill="red")
connection_indicator.pack(side=tk.LEFT)

# Create a label for the connection status inside the connection frame
connection_status = tk.Label(connection_frame, text="Plex Connection: Unknown", fg="red")
connection_status.pack(side=tk.LEFT)

# Get an authentication token from Plex
def plex_login():
   # Create a form with Plex Username and Password
    plex_account_name.set(simpledialog.askstring("Plex Login", "Enter your Plex username"))
    plex_password.set(simpledialog.askstring("Plex Login", "Enter your Plex password", show="*"))
    try:
        account = MyPlexAccount(plex_account_name.get(), plex_password.get())
        plex_token.set(account.authenticationToken)
    except Unauthorized:
        messagebox.showerror("Plex Login", "Invalid username or password")
        return
    save_settings()
    test_plex_connection()

# Create a "Test Connection" button
test_connection_button = tk.Button(frame, text="Plex Login", command=plex_login)
test_connection_button.pack(pady=10)

# Create a label for the progress
progress_label = tk.Label(frame, text="Progress: 0 / 0 \n ")
progress_label.pack(pady=5)

# Add the progress bar, start button, and menu bar below the new widgets
progress = ttk.Progressbar(frame, length=100, mode='determinate')
progress.pack(fill=tk.X, expand=1, pady=5)

start_button = tk.Button(frame, text="Start Scan", command=start_scan)
if not plex_connected:
    start_button.config(state=tk.DISABLED)
start_button.pack(pady=10)

# Create a variable for the selected library
selected_library = tk.StringVar()

# Create a label for the library dropdown menu
library_label = tk.Label(frame, text="Library")
library_label.pack(pady=5)

# Create a dropdown menu of Plex libraries
libraries = []
if test_plex_connection():
    libraries = get_libraries()
library_menu = ttk.Combobox(frame, textvariable=selected_library)
library_menu['values'] = [library.title for library in libraries]
library_menu.pack(pady=10)
def on_library_select(event):
    start_button.config(state=tk.NORMAL)

library_menu.bind("<<ComboboxSelected>>", on_library_select)
if not plex_connected:
    library_menu.config(state="disabled")

# Create a frame for the Results group
results_frame = tk.LabelFrame(frame, text="Results", height=600)
results_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

# Create a table for the Results group
results_table = ttk.Treeview(results_frame, columns=("Library", "Title", "Plex Link", "Disk Link", "Problems"), show="headings")
results_table.heading("Library", text="Library")
results_table.heading("Title", text="Title")
results_table.heading("Plex Link", text="Plex Location")
results_table.heading("Disk Link", text="Disk Location")
results_table.heading("Problems", text="Problems")
# Set the width of each column
results_table.column("Library", width=int(total_width * 0.10))
results_table.column("Title", width=int(total_width * 0.25))
results_table.column("Plex Link", width=int(total_width * 0.10))
results_table.column("Disk Link", width=int(total_width * 0.10))
results_table.column("Problems", width=int(total_width * 0.45))
results_table.pack(fill=tk.BOTH, expand=True)
# Create two tags with different background colors
results_table.tag_configure("oddrow", background="white")
results_table.tag_configure("evenrow", background="light gray")

# Add a scrollbar
scrollbar = ttk.Scrollbar(results_frame, orient="vertical", command=results_table.yview)
scrollbar.pack(side="right", fill="y")
results_table.configure(yscrollcommand=scrollbar.set)

# Create a menu bar
menubar = Menu(root)
root.config(menu=menubar)
# Create a settings menu
settings_menu = Menu(menubar, tearoff=0)

# Add settings menu to the menu bar
menubar.add_cascade(label="Settings", menu=settings_menu)

# Add settings options to the settings menu
settings_menu.add_checkbutton(label="Check for Credits", onvalue=True, offvalue=False, variable=check_credits)
settings_menu.add_checkbutton(label="Validate Bitrate", onvalue=True, offvalue=False, variable=validate_bitrate)
settings_menu.add_checkbutton(label="Check for missing episodes", onvalue=True, offvalue=False, variable=check_missing_episodes) # Added 'check_missing_episodes' to the settings menu
settings_menu.add_checkbutton(label="Don't allow .ts files", onvalue=True, offvalue=False, variable=no_ts) # Added 'no_ts' to the settings menu
settings_menu.add_checkbutton(label="Ignore .ts files", onvalue=True, offvalue=False, variable=ignore_ts) # Added 'no_ts' to the settings menu
settings_menu.add_checkbutton(label="Check for encoding errors (very slow)", onvalue=True, offvalue=False, variable=check_encoding_errors)

def check_corrupt_video(filepath):
    try:
        # Run ffmpeg command to check if the video file is corrupt
        subprocess.check_output(['ffmpeg', '-v', 'error', '-i', filepath, '-f', 'null', '-'], stderr=subprocess.STDOUT)
        return False  # Video file is not corrupt
    except subprocess.CalledProcessError as e:
        return True  # Video file is corrupt



root.mainloop()