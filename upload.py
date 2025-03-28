#!/usr/bin/env python3
from src.args import Args
from src.clients import Clients
from src.uploadscreens import upload_screens
import json
from pathlib import Path
import asyncio
import os
import sys
import platform
import shutil
import cli_ui
import traceback
import time
import gc
import subprocess
import re
import requests
from packaging import version
from src.trackersetup import tracker_class_map, api_trackers, other_api_trackers, http_trackers
from src.trackerhandle import process_trackers
from src.queuemanage import handle_queue
from src.console import console
from src.torrentcreate import create_torrent, create_random_torrents, create_base_from_existing_torrent
from src.uphelper import UploadHelper
from src.trackerstatus import process_all_trackers
from src.takescreens import disc_screenshots, dvd_screenshots, screenshots
from src.cleanup import cleanup
if os.name == "posix":
    import termios


cli_ui.setup(color='always', title="Audionut's Upload Assistant")
running_subprocesses = set()
base_dir = os.path.abspath(os.path.dirname(__file__))

try:
    from data.config import config
except Exception:
    if not os.path.exists(os.path.abspath(f"{base_dir}/data/config.py")):
        cli_ui.info(cli_ui.red, "Configuration file 'config.py' not found.")
        cli_ui.info(cli_ui.red, "Please ensure the file is located at:", cli_ui.yellow, os.path.abspath(f"{base_dir}/data/config.py"))
        cli_ui.info(cli_ui.red, "Follow the setup instructions: https://github.com/Audionut/Upload-Assistant")
        exit()
    else:
        console.print(traceback.print_exc())

from src.prep import Prep  # noqa E402
client = Clients(config=config)
parser = Args(config)


async def merge_meta(meta, saved_meta, path):
    """Merges saved metadata with the current meta, respecting overwrite rules."""
    with open(f"{base_dir}/tmp/{os.path.basename(path)}/meta.json") as f:
        saved_meta = json.load(f)
        overwrite_list = [
            'trackers', 'dupe', 'debug', 'anon', 'category', 'type', 'screens', 'nohash', 'manual_edition', 'imdb', 'tmdb_manual', 'mal', 'manual',
            'hdb', 'ptp', 'blu', 'no_season', 'no_aka', 'no_year', 'no_dub', 'no_tag', 'no_seed', 'client', 'desclink', 'descfile', 'desc', 'draft',
            'modq', 'region', 'freeleech', 'personalrelease', 'unattended', 'manual_season', 'manual_episode', 'torrent_creation', 'qbit_tag', 'qbit_cat',
            'skip_imghost_upload', 'imghost', 'manual_source', 'webdv', 'hardcoded-subs', 'dual_audio', 'manual_type', 'tvmaze_manual'
        ]
        sanitized_saved_meta = {}
        for key, value in saved_meta.items():
            clean_key = key.strip().strip("'").strip('"')
            if clean_key in overwrite_list:
                if clean_key in meta and meta.get(clean_key) is not None:
                    sanitized_saved_meta[clean_key] = meta[clean_key]
                    if meta['debug']:
                        console.print(f"Overriding {clean_key} with meta value:", meta[clean_key])
                else:
                    sanitized_saved_meta[clean_key] = value
            else:
                sanitized_saved_meta[clean_key] = value
        meta.update(sanitized_saved_meta)
    f.close()
    return sanitized_saved_meta


async def process_meta(meta, base_dir):
    """Process the metadata for each queued path."""

    if meta['imghost'] is None:
        meta['imghost'] = config['DEFAULT']['img_host_1']

    if not meta['unattended']:
        ua = config['DEFAULT'].get('auto_mode', False)
        if str(ua).lower() == "true":
            meta['unattended'] = True
            console.print("[yellow]Running in Auto Mode")
    meta['base_dir'] = base_dir
    prep = Prep(screens=meta['screens'], img_host=meta['imghost'], config=config)
    try:
        meta = await prep.gather_prep(meta=meta, mode='cli')
    except Exception as e:
        console.print(f"Error in gather_prep: {e}")
        console.print(traceback.format_exc())
        return
    meta['name_notag'], meta['name'], meta['clean_name'], meta['potential_missing'] = await prep.get_name(meta)
    parser = Args(config)
    helper = UploadHelper()
    if meta.get('trackers'):
        trackers = meta['trackers']
    else:
        default_trackers = config['TRACKERS'].get('default_trackers', '')
        trackers = [tracker.strip() for tracker in default_trackers.split(',')]

    if isinstance(trackers, str):
        if "," in trackers:
            trackers = [t.strip().upper() for t in trackers.split(',')]
        else:
            trackers = [trackers.strip().upper()]  # Make it a list with one element
    else:
        trackers = [t.strip().upper() for t in trackers]
    meta['trackers'] = trackers
    with open(f"{meta['base_dir']}/tmp/{meta['uuid']}/meta.json", 'w') as f:
        json.dump(meta, f, indent=4)
        f.close()
    confirm = await helper.get_confirmation(meta)
    while confirm is False:
        editargs = cli_ui.ask_string("Input args that need correction e.g. (--tag NTb --category tv --tmdb 12345)")
        editargs = (meta['path'],) + tuple(editargs.split())
        if meta.get('debug', False):
            editargs += ("--debug",)
        if meta.get('trackers', None) is not None:
            editargs += ("--trackers", ",".join(meta["trackers"]))
        meta, help, before_args = parser.parse(editargs, meta)
        if isinstance(meta.get('trackers'), str):
            if "," in meta['trackers']:
                meta['trackers'] = [t.strip() for t in meta['trackers'].split(',')]
            else:
                meta['trackers'] = [meta['trackers']]
        meta['edit'] = True
        meta = await prep.gather_prep(meta=meta, mode='cli')
        meta['name_notag'], meta['name'], meta['clean_name'], meta['potential_missing'] = await prep.get_name(meta)
        confirm = await helper.get_confirmation(meta)

    successful_trackers = await process_all_trackers(meta)

    if meta.get('trackers_pass') is not None:
        meta['skip_uploading'] = meta.get('trackers_pass')
    else:
        meta['skip_uploading'] = int(config['DEFAULT'].get('tracker_pass_checks', 1))
    if successful_trackers < int(meta['skip_uploading']) and not meta['debug']:
        console.print(f"[red]Not enough successful trackers ({successful_trackers}/{meta['skip_uploading']}). EXITING........[/red]")

    else:
        meta['we_are_uploading'] = True
        filename = meta.get('title', None)
        bdmv_filename = meta.get('filename', None)
        bdinfo = meta.get('bdinfo', None)
        videopath = meta.get('filelist', [None])
        videopath = videopath[0] if videopath else None
        console.print(f"Processing {filename} for upload")
        if 'manual_frames' not in meta:
            meta['manual_frames'] = {}
        manual_frames = meta['manual_frames']
        # Take Screenshots
        try:
            if meta['is_disc'] == "BDMV":
                use_vs = meta.get('vapoursynth', False)
                try:
                    await disc_screenshots(
                        meta, bdmv_filename, bdinfo, meta['uuid'], base_dir, use_vs,
                        meta.get('image_list', []), meta.get('ffdebug', False), None
                    )
                except asyncio.CancelledError:
                    console.print("[red]Screenshot capture was cancelled. Cleaning up...[/red]")
                    await cleanup_screenshot_temp_files(meta)  # Cleanup only on cancellation
                    raise  # Ensure cancellation propagates properly
                except Exception as e:
                    console.print(f"[red]Error during BDMV screenshot capture: {e}[/red]", highlight=False)
                    await cleanup_screenshot_temp_files(meta)  # Cleanup only on error

            elif meta['is_disc'] == "DVD":
                try:
                    await dvd_screenshots(
                        meta, 0, None, None
                    )
                except asyncio.CancelledError:
                    console.print("[red]DVD screenshot capture was cancelled. Cleaning up...[/red]")
                    await cleanup_screenshot_temp_files(meta)
                    raise
                except Exception as e:
                    console.print(f"[red]Error during DVD screenshot capture: {e}[/red]", highlight=False)
                    await cleanup_screenshot_temp_files(meta)

            else:
                try:
                    if meta['debug']:
                        console.print(f"videopath: {videopath}, filename: {filename}, meta: {meta['uuid']}, base_dir: {base_dir}, manual_frames: {manual_frames}")

                    await screenshots(
                        videopath, filename, meta['uuid'], base_dir, meta,
                        manual_frames=manual_frames  # Pass additional kwargs directly
                    )
                except asyncio.CancelledError:
                    console.print("[red]Generic screenshot capture was cancelled. Cleaning up...[/red]")
                    await cleanup_screenshot_temp_files(meta)
                    raise
                except Exception as e:
                    console.print(f"[red]Error during generic screenshot capture: {e}[/red]", highlight=False)
                    await cleanup_screenshot_temp_files(meta)

        except asyncio.CancelledError:
            console.print("[red]Process was cancelled. Performing cleanup...[/red]")
            await cleanup_screenshot_temp_files(meta)
            raise
        except Exception as e:
            console.print(f"[red]Unexpected error occurred: {e}[/red]")
            await cleanup_screenshot_temp_files(meta)
        finally:
            await asyncio.sleep(0.1)
            await cleanup()
            gc.collect()
            reset_terminal()

        meta['cutoff'] = int(config['DEFAULT'].get('cutoff_screens', 1))
        if 'image_list' not in meta:
            meta['image_list'] = []
        if len(meta.get('image_list', [])) < meta.get('cutoff') and meta.get('skip_imghost_upload', False) is False:
            return_dict = {}
            try:
                new_images, dummy_var = await upload_screens(
                    meta, meta['screens'], 1, 0, meta['screens'], [], return_dict=return_dict
                )
            except asyncio.CancelledError:
                console.print("\n[red]Upload process interrupted! Cancelling tasks...[/red]")
                return
            except Exception as e:
                console.print(f"\n[red]Unexpected error during upload: {e}[/red]")
            finally:
                reset_terminal()
                console.print("[yellow]Cleaning up resources...[/yellow]")
                gc.collect()

        elif meta.get('skip_imghost_upload', False) is True and meta.get('image_list', False) is False:
            meta['image_list'] = []

        with open(f"{meta['base_dir']}/tmp/{meta['uuid']}/meta.json", 'w') as f:
            json.dump(meta, f, indent=4)

        if not meta['mkbrr']:
            meta['mkbrr'] = int(config['DEFAULT'].get('mkbrr', False))
        torrent_path = os.path.abspath(f"{meta['base_dir']}/tmp/{meta['uuid']}/BASE.torrent")
        if not os.path.exists(torrent_path):
            reuse_torrent = None
            if meta.get('rehash', False) is False:
                reuse_torrent = await client.find_existing_torrent(meta)
                if reuse_torrent is not None:
                    await create_base_from_existing_torrent(reuse_torrent, meta['base_dir'], meta['uuid'])

            if meta['nohash'] is False and reuse_torrent is None:
                create_torrent(meta, Path(meta['path']), "BASE")
            if meta['nohash']:
                meta['client'] = "none"

        elif os.path.exists(torrent_path) and meta.get('rehash', False) is True and meta['nohash'] is False:
            create_torrent(meta, Path(meta['path']), "BASE")

        if int(meta.get('randomized', 0)) >= 1:
            create_random_torrents(meta['base_dir'], meta['uuid'], meta['randomized'], meta['path'])

        if 'saved_description' in meta and meta['saved_description'] is False:
            meta = await prep.gen_desc(meta)
        else:
            meta = await prep.gen_desc(meta)

        if meta.get('description') in ('None', '', ' '):
            meta['description'] = None

        with open(f"{meta['base_dir']}/tmp/{meta['uuid']}/meta.json", 'w') as f:
            json.dump(meta, f, indent=4)


async def cleanup_screenshot_temp_files(meta):
    """Cleanup temporary screenshot files to prevent orphaned files in case of failures."""
    tmp_dir = f"{meta['base_dir']}/tmp/{meta['uuid']}"
    if os.path.exists(tmp_dir):
        try:
            for file in os.listdir(tmp_dir):
                file_path = os.path.join(tmp_dir, file)
                if os.path.isfile(file_path) and file.endswith((".png", ".jpg")):
                    os.remove(file_path)
                    if meta['debug']:
                        console.print(f"[yellow]Removed temporary screenshot file: {file_path}[/yellow]")
        except Exception as e:
            console.print(f"[red]Error cleaning up temporary screenshot files: {e}[/red]", highlight=False)


async def get_log_file(base_dir, queue_name):
    """
    Returns the path to the log file for the given base directory and queue name.
    """
    safe_queue_name = queue_name.replace(" ", "_")
    return os.path.join(base_dir, "tmp", f"{safe_queue_name}_processed_files.log")


async def load_processed_files(log_file):
    """
    Loads the list of processed files from the log file.
    """
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            return set(json.load(f))
    return set()


async def save_processed_file(log_file, file_path):
    """
    Adds a processed file to the log.
    """
    processed_files = await load_processed_files(log_file)
    processed_files.add(file_path)
    with open(log_file, "w") as f:
        json.dump(list(processed_files), f, indent=4)


def reset_terminal():
    """Reset the terminal while allowing the script to continue running (Linux/macOS only)."""
    if os.name != "posix":
        return

    try:
        if not sys.stderr.closed:
            sys.stderr.flush()

        if hasattr(sys.stdin, 'isatty') and sys.stdin.isatty() and not sys.stdin.closed:
            try:
                subprocess.run(["stty", "sane"], check=False)
                if hasattr(termios, 'tcflush'):
                    termios.tcflush(sys.stdin.fileno(), termios.TCIOFLUSH)
                subprocess.run(["stty", "-ixon"], check=False)
            except (IOError, OSError):
                pass

        if not sys.stdout.closed:
            try:
                sys.stdout.write("\033[0m")
                sys.stdout.flush()
                sys.stdout.write("\033[?25h")
                sys.stdout.flush()
            except (IOError, ValueError):
                pass

        # Kill background jobs
        try:
            os.system("jobs -p | xargs -r kill 2>/dev/null")
        except Exception:
            pass

        if not sys.stderr.closed:
            sys.stderr.flush()

    except Exception as e:
        try:
            if not sys.stderr.closed:
                sys.stderr.write(f"Error during terminal reset: {e}\n")
                sys.stderr.flush()
        except Exception:
            pass  # At this point we can't do much more


def get_local_version(version_file):
    """Extracts the local version from the version.py file."""
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            content = f.read()
        match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
        if match:
            console.print(f"[cyan]Version[/cyan] [yellow]{match.group(1)}")
            return match.group(1)
        else:
            console.print("[red]Version not found in local file.")
            return None
    except FileNotFoundError:
        console.print("[red]Version file not found.")
        return None


def get_remote_version(url):
    """Fetches the latest version information from the remote repository."""
    try:
        response = requests.get(url)
        if response.status_code == 200:
            content = response.text
            match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
            if match:
                return match.group(1), content
            else:
                console.print("[red]Version not found in remote file.")
                return None, None
        else:
            console.print(f"[red]Failed to fetch remote version file. Status code: {response.status_code}")
            return None, None
    except requests.RequestException as e:
        console.print(f"[red]An error occurred while fetching the remote version file: {e}")
        return None, None


def extract_changelog(content, from_version, to_version):
    """Extracts the changelog entries between the specified versions."""
    pattern = rf'__version__\s*=\s*"{re.escape(to_version)}"\s*(.*?)__version__\s*=\s*"{re.escape(from_version)}"'
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return match.group(1).strip()
    else:
        return None


async def update_notification(base_dir):
    version_file = os.path.join(base_dir, 'data', 'version.py')
    remote_version_url = 'https://raw.githubusercontent.com/Audionut/Upload-Assistant/master/data/version.py'

    notice = config['DEFAULT'].get('update_notification', True)
    verbose = config['DEFAULT'].get('verbose_notification', False)

    if not notice:
        return

    local_version = get_local_version(version_file)
    if not local_version:
        return

    remote_version, remote_content = get_remote_version(remote_version_url)
    if not remote_version:
        return

    if version.parse(remote_version) > version.parse(local_version):
        console.print(f"[red][NOTICE] [green]Update available: v[/green][yellow]{remote_version}")
        asyncio.create_task(asyncio.sleep(1))
        if verbose and remote_content:
            changelog = extract_changelog(remote_content, local_version, remote_version)
            if changelog:
                asyncio.create_task(asyncio.sleep(1))
                console.print(f"{changelog}")
            else:
                console.print("[yellow]Changelog not found between versions.[/yellow]")


async def do_the_thing(base_dir):
    await asyncio.sleep(0.1)  # Ensure it's not racing
    meta = dict()
    paths = []
    for each in sys.argv[1:]:
        if os.path.exists(each):
            paths.append(os.path.abspath(each))
        else:
            break

    await update_notification(base_dir)

    cleanup_only = any(arg in ('--cleanup', '-cleanup') for arg in sys.argv) and len(sys.argv) <= 2

    try:
        # If cleanup is the only operation, use a dummy path to satisfy the parser
        if cleanup_only:
            args_list = sys.argv[1:] + ['dummy_path']
            meta, help, before_args = parser.parse(tuple(' '.join(args_list).split(' ')), meta)
            meta['path'] = None  # Clear the dummy path after parsing
        else:
            meta, help, before_args = parser.parse(tuple(' '.join(sys.argv[1:]).split(' ')), meta)

        if meta.get('cleanup'):
            if os.path.exists(f"{base_dir}/tmp"):
                shutil.rmtree(f"{base_dir}/tmp")
                console.print("[bold green]Successfully emptied tmp directory")
            if not meta.get('path') or cleanup_only:
                exit(0)

        if not meta.get('path'):
            exit(0)

        path = meta['path']
        path = os.path.abspath(path)
        if path.endswith('"'):
            path = path[:-1]

        queue, log_file = await handle_queue(path, meta, paths, base_dir)

        processed_files_count = 0
        base_meta = {k: v for k, v in meta.items()}
        for path in queue:
            total_files = len(queue)
            try:
                meta = base_meta.copy()
                meta['path'] = path
                meta['uuid'] = None

                if not path:
                    raise ValueError("The 'path' variable is not defined or is empty.")

                meta_file = os.path.join(base_dir, "tmp", os.path.basename(path), "meta.json")

                if meta.get('delete_meta') and os.path.exists(meta_file):
                    os.remove(meta_file)
                    console.print("[bold red]Successfully deleted meta.json")

                if os.path.exists(meta_file):
                    with open(meta_file, "r") as f:
                        saved_meta = json.load(f)
                        console.print("[yellow]Existing metadata file found, it holds cached values")
                        meta.update(await merge_meta(meta, saved_meta, path))
                else:
                    if meta['debug']:
                        console.print(f"[yellow]No metadata file found at {meta_file}")

            except Exception as e:
                console.print(f"[red]Failed to load metadata for path '{path}': {e}")
                reset_terminal()

            if meta['debug']:
                start_time = time.time()

            if meta['read_release_notes']:
                # Read notes from STDIN
                print("> Enter release notes (Ctrl-D to finish):")
                meta['release_notes'] = sys.stdin.read()
                
            console.print(f"[green]Gathering info for {os.path.basename(path)}")
            await process_meta(meta, base_dir)

            if 'we_are_uploading' not in meta:
                console.print("we are not uploading.......")
                if 'queue' in meta and meta.get('queue') is not None:
                    processed_files_count += 1
                    console.print(f"[cyan]Processed {processed_files_count}/{total_files} files.")
                    if not meta['debug']:
                        if log_file:
                            await save_processed_file(log_file, path)

            else:
                await process_trackers(meta, config, client, console, api_trackers, tracker_class_map, http_trackers, other_api_trackers)
                if 'queue' in meta and meta.get('queue') is not None:
                    processed_files_count += 1
                    console.print(f"[cyan]Processed {processed_files_count}/{total_files} files.")
                    if not meta['debug']:
                        if log_file:
                            await save_processed_file(log_file, path)

            if 'limit_queue' in meta and meta['limit_queue'] > 0:
                if processed_files_count >= meta['limit_queue']:
                    console.print(f"[red]Processing limit of {meta['limit_queue']} files reached. Stopping queue processing.")
                    break

            if meta['debug']:
                finish_time = time.time()
                console.print(f"Uploads processed in {finish_time - start_time:.4f} seconds")

    except Exception as e:
        console.print(f"[bold red]An unexpected error occurred: {e}")
        console.print(traceback.format_exc())
        reset_terminal()

    finally:
        if not sys.stdin.closed:
            reset_terminal()


def check_python_version():
    pyver = platform.python_version_tuple()
    if int(pyver[0]) != 3 or int(pyver[1]) < 9:
        console.print("[bold red]Python version is too low. Please use Python 3.9 or higher.")
        sys.exit(1)


async def main():
    try:
        await do_the_thing(base_dir)  # Ensure base_dir is correctly defined
    except asyncio.CancelledError:
        console.print("[red]Tasks were cancelled. Exiting safely.[/red]")
    except KeyboardInterrupt:
        console.print("[bold red]Program interrupted. Exiting safely.[/bold red]")
    except Exception as e:
        console.print(f"[bold red]Unexpected error: {e}[/bold red]")
    finally:
        await cleanup()
        reset_terminal()


if __name__ == "__main__":
    check_python_version()

    try:
        # Use ProactorEventLoop for Windows subprocess handling
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

        asyncio.run(main())  # Ensures proper loop handling and cleanup
    except (KeyboardInterrupt, SystemExit):
        pass
    except BaseException as e:
        console.print(f"[bold red]Critical error: {e}[/bold red]")
    finally:
        asyncio.run(cleanup())
        reset_terminal()
        sys.exit(0)
