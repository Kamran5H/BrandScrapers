import pandas as pd
import tkinter as tk
from tkinter import filedialog
import os
import re

ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")

def extract_unique_asins():
    root = tk.Tk()
    root.withdraw()
    # Ensure dialog is visible on top
    root.attributes('-topmost', True)

    folder_selected = filedialog.askdirectory(title="Select Folder with CSVs")
    root.destroy()

    if not folder_selected:
        print("No folder selected.")
        return

    # Use os.listdir to be explicit and catch case variations like .CSV
    all_files = [
        os.path.join(folder_selected, f) 
        for f in os.listdir(folder_selected) 
        if f.lower().endswith('.csv')
    ]
    unique_asins = set()

    print(f"Reading {len(all_files)} files...")

    for file in all_files:
        filename = os.path.basename(file)

        # Case-insensitive check to skip the master file
        if filename.lower() == "unique_asins_master.csv":
            continue

        try:
            # We use header=None so we don't skip the first row if the CSV doesn't have a header.
            # usecols=[1] assumes the file has at least 2 columns in its first row.
            df = pd.read_csv(
                file,
                usecols=[1],
                header=None,
                dtype=str,
                encoding="utf-8",
                encoding_errors="replace",
                on_bad_lines="skip"
            )

            if df.empty:
                print(f"Skipped empty data: {filename}")
                continue

            column_b = (
                df.iloc[:, 0]
                .dropna()
                .astype(str)
                .str.strip()
                .str.upper()
            )

            valid_asins = column_b[column_b.str.match(ASIN_PATTERN)]
            unique_asins.update(valid_asins.tolist())

            print(f"Processed: {filename} | Found valid ASINs: {len(valid_asins)}")

        except pd.errors.EmptyDataError:
            print(f"Skipped empty file: {filename}")

        except ValueError:
            print(f"Skipped file with no Column B: {filename}")

        except Exception as e:
            print(f"Error reading {filename}: {e}")

    if unique_asins:
        master_df = pd.DataFrame(sorted(unique_asins), columns=["ASIN"])
        output_path = os.path.join(folder_selected, "Unique_ASINs_Master.csv")
        
        try:
            master_df.to_csv(output_path, index=False, encoding="utf-8-sig")
            print(f"\nDone! {len(master_df)} unique ASINs saved to:")
            print(output_path)
        except PermissionError:
            print(f"\nERROR: Could not save the file.")
            print(f"Please close '{output_path}' if it is open in Excel or another program, and run the script again.")
        except Exception as e:
            print(f"\nERROR saving file: {e}")
    else:
        print("\nNo valid ASINs found in any of the files.")

if __name__ == "__main__":
    extract_unique_asins()