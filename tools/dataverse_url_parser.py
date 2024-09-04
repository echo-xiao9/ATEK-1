import argparse
import json
import logging
import os
import random
from itertools import islice
from typing import Dict, List, Optional, Tuple

import requests
import yaml
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)s:%(message)s",  # Format of the log messages
    handlers=[
        logging.StreamHandler(),  # Output logs to console
    ],
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def save_yaml(data: Dict, file_path: str):
    import yaml

    with open(file_path, "w") as file:
        yaml.dump(data, file, default_flow_style=False)


def split_train_val_sequences(
    sequences_tar_info_dict: Dict[str, Dict[str, str]], ratio: float, seed: int
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, str]]]:
    """
    Given a dictionary of tar URLs, split it into train and validation sets based on the specified ratio and random seed.
    """
    random.seed(seed)
    sequences = list(sequences_tar_info_dict.keys())
    sequences.sort()
    random.shuffle(sequences)
    split_point = int(len(sequences) * ratio)
    train_sequences = sequences[:split_point]
    valid_sequences = sequences[split_point:]
    train_sequences_tar_info_dict = {
        sequence: sequences_tar_info_dict[sequence] for sequence in train_sequences
    }
    validation_sequences_tar_info_dict = {
        sequence: sequences_tar_info_dict[sequence] for sequence in valid_sequences
    }
    return train_sequences_tar_info_dict, validation_sequences_tar_info_dict


def extract_first_K_sequences(
    wds_file_urls: Dict, max_num_sequences: Optional[int] = None
) -> Dict:
    """
    Extract tar info from the given dictionary of WDS file info.
    If max_num_sequences is specified, only the first max_num_sequences sequences will be included.
    """
    # if no cropping is needed
    if not max_num_sequences or max_num_sequences < 0:
        return wds_file_urls
    else:
        return dict(islice(wds_file_urls.items(), max_num_sequences))


def download_wds_files_for_single_sequence(
    urls: Dict[str, Dict[str, str]], output_dir: str, sequence_name: str
) -> Tuple[List[str], List[str]]:
    """
    For each sequence, download all the files to a directory with the same name.
    Return a dictionary mapping sequence names to lists of downloaded file paths.
    """
    successful_urls = []
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    # Set up retry strategy
    retries = Retry(
        total=5,
        backoff_factor=2,  # final try is 2*(2**5) = 64 seconds
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retries)
    http = requests.Session()
    http.mount("http://", adapter)
    http.mount("https://", adapter)
    failed_urls = []
    for tar_name, tar_info in urls.items():
        # original tar name is shards-0000_tar, we need to change to shards-0000.tar
        tar_name = tar_name.replace("_tar", ".tar")
        filepath = os.path.join(output_dir, tar_name)
        relative_path = os.path.join(sequence_name, tar_name)
        # Initialize the list for this sequence if not already done
        try:
            response = http.get(tar_info["download_url"], stream=True)
            response.raise_for_status()  # Raise an exception for bad status codes
            with open(filepath, "wb") as f:
                for chunk in response.iter_content(1024):
                    f.write(chunk)
            successful_urls.append(relative_path)
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to download {relative_path}: {e}")
            failed_urls.append(relative_path)
    return successful_urls, failed_urls


def download_sequences(
    download_sequences_tar_info_dict: Dict[str, Dict[str, str]], output_folder_path: str
) -> Tuple[Dict, Dict]:
    """
    Each sequence will be downloaded into a new directory named after the sequence within the specified output folder.
    The function logs the download progress, creates necessary directories, and uses the `download_wds_files_for_single_sequence` function
    to actually download the files.
    """
    success_sequences_with_tars = {}
    failed_sequences_with_tars = {}
    # Wrap the loop with tqdm for a progress bar
    for sequence_name, urls in tqdm(
        download_sequences_tar_info_dict.items(), desc="Downloading sequences"
    ):
        logger.info(f"Downloading {sequence_name}")
        sequence_dir = os.path.join(output_folder_path, sequence_name)
        os.makedirs(
            sequence_dir, exist_ok=True
        )  # Added exist_ok=True to avoid error if directory already exists
        successful_tar_list, failed_tar_list = download_wds_files_for_single_sequence(
            urls, sequence_dir, sequence_name
        )
        success_sequences_with_tars[sequence_name] = successful_tar_list
        if len(failed_tar_list) > 0:
            failed_sequences_with_tars[sequence_name] = failed_tar_list
    return failed_sequences_with_tars, success_sequences_with_tars


def get_url_from_tars_info(tars_info: Dict) -> Dict:
    # extract tar urls from tars_info, which is a dictionary
    tar_urls = {}
    for sequence_name, sequence_tars_info in tars_info.items():
        tar_urls[sequence_name] = [
            tar_info["download_url"] for tar_info in sequence_tars_info.values()
        ]
    return tar_urls


def main(
    config_name: str,
    input_json_path: str,
    train_val_split_ratio: float,
    random_seed: int,
    output_folder_path: str,
    max_num_sequences: Optional[int] = None,
    download_wds_to_local: bool = False,
):
    assert os.path.exists(
        input_json_path
    ), f"Input JSON file {input_json_path} does not exist."
    assert (
        os.path.exists(output_folder_path) is False
    ), f"Output folder {output_folder_path} already exists."

    with open(input_json_path, "r") as file:
        data = json.load(file)
    sequence_to_tar_info = data["atek_data_for_all_configs"][config_name][
        "wds_file_urls"
    ]
    sequences_tar_info_dict = extract_first_K_sequences(
        sequence_to_tar_info, max_num_sequences
    )

    train_sequences_tar_info_dict, validation_sequences_tar_info_dict = (
        split_train_val_sequences(
            sequences_tar_info_dict, train_val_split_ratio, random_seed
        )
    )
    # Save the tars failed to download
    if not os.path.exists(output_folder_path):
        os.makedirs(output_folder_path)
    if download_wds_to_local is True:
        # Download sequences to local
        failed_train_sequence_with_tars, successful_train_sequences_with_tars = (
            download_sequences(train_sequences_tar_info_dict, output_folder_path)
        )
        (
            failed_validation_sequence_with_tars,
            successful_validation_sequences_with_tars,
        ) = download_sequences(validation_sequences_tar_info_dict, output_folder_path)

        successful_all_sequences_with_tars = (
            successful_train_sequences_with_tars
            | successful_validation_sequences_with_tars
        )
        failed_sequences_with_tars = (
            failed_train_sequence_with_tars | failed_validation_sequence_with_tars
        )

        # Save the YAML files
        with open(
            os.path.join(output_folder_path, "local_train_tars.yaml"), "w"
        ) as file:
            yaml.dump(
                {"tars": successful_train_sequences_with_tars},
                file,
                default_flow_style=False,
            )
        with open(
            os.path.join(output_folder_path, "local_validation_tars.yaml"), "w"
        ) as file:
            yaml.dump(
                {"tars": successful_validation_sequences_with_tars},
                file,
                default_flow_style=False,
            )
        with open(os.path.join(output_folder_path, "local_all_tars.yaml"), "w") as file:
            yaml.dump(
                {"tars": successful_all_sequences_with_tars},
                file,
                default_flow_style=False,
            )
        logger.info(
            f"Successfully download {len(successful_all_sequences_with_tars)} sequences."
        )
        logger.info(f"Failed to download {len(failed_sequences_with_tars)} sequences.")
        for sequence_name, failed_tars in failed_sequences_with_tars.items():
            logger.info(f"Failed to download {sequence_name}: {len(failed_tars)} tars.")

    else:
        # original train_tars, valid_tars and tar_urls save the file name, sha1sum, file size and download url, now we only need the download url
        train_sequences_tar_url = get_url_from_tars_info(train_sequences_tar_info_dict)
        validation_sequences_tar_url = get_url_from_tars_info(
            validation_sequences_tar_info_dict
        )
        all_sequences_tar_url = get_url_from_tars_info(sequences_tar_info_dict)
        with open(
            os.path.join(output_folder_path, "streamable_train_tars.yaml"), "w"
        ) as file:
            yaml.dump({"tars": train_sequences_tar_url}, file, default_flow_style=False)
        with open(
            os.path.join(output_folder_path, "streamable_validation_tars.yaml"), "w"
        ) as file:
            yaml.dump(
                {"tars": validation_sequences_tar_url}, file, default_flow_style=False
            )
        with open(
            os.path.join(output_folder_path, "streamable_all_tars.yaml"), "w"
        ) as file:
            yaml.dump({"tars": all_sequences_tar_url}, file, default_flow_style=False)


def get_args():
    parser = argparse.ArgumentParser(
        description="Parse dataverse urls to generate yaml file for training, validation and combined"
    )
    parser.add_argument("--config-name", type=str, help="Configuration name")
    parser.add_argument(
        "--input-json-path", type=str, help="Path to the input JSON file"
    )
    parser.add_argument(
        "--train-val-split-ratio",
        type=float,
        default=0.9,
        help="Train-validation split ratio",
    )
    parser.add_argument(
        "--random-seed", type=int, default=42, help="Random seed for shuffling data"
    )
    parser.add_argument(
        "--output-folder-path",
        type=str,
        help="Output folder path for YAML files and downloaded wds",
    )
    parser.add_argument(
        "--download-wds-to-local", action="store_true", help="Download WDS files"
    )
    parser.add_argument(
        "--max-num-sequences", type=int, help="Maximum number of sequences"
    )
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = get_args()
    main(
        args.config_name,
        args.input_json_path,
        args.train_val_split_ratio,
        args.random_seed,
        args.output_folder_path,
        args.max_num_sequences,
        args.download_wds_to_local,
    )
