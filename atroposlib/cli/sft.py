import argparse
import asyncio
import os

import aiohttp
import jsonlines
from tqdm.asyncio import tqdm  # Import tqdm for async
from transformers import AutoTokenizer

from atroposlib.utils.io import parse_http_response


def find_common_prefix(strings):
    """
    Finds the longest common prefix among a list of strings.

    Args:
        strings: A list of strings.

    Returns:
        The longest common prefix string, or an empty string if the list is empty
        or no common prefix exists.
    """
    if not strings:
        return ""

    prefix = strings[0]
    for s in strings[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


async def register_to_api(
    group_size, max_token_len, api_url, num_steps, tasks_per_step
):
    """
    Registers this data grabber instance with the Atropos API.

    This involves resetting any previous data on the server and then sending
    configuration parameters for the current session.

    Args:
        group_size: The number of sequences processed per group by the API.
        max_token_len: The maximum token length for sequences.
        api_url: The base URL of the Atropos API server.
        num_steps: The number of steps to run the API for.
        tasks_per_step: The number of tasks per step for batch size calculation.
    """
    async with aiohttp.ClientSession() as session:
        # Reset data on the API server before registering
        async with session.get(f"{api_url}/reset_data") as response:
            print(await response.text())
        # Register this instance with its configuration
        async with session.post(
            f"{api_url}/register",
            json={
                "wandb_group": "test",
                "wandb_project": "test",
                "batch_size": group_size * tasks_per_step,
                "max_token_len": max_token_len,
                "checkpoint_dir": "checkpoints",
                "save_checkpoint_interval": 10,
                "starting_step": 0,
                "num_steps": num_steps * 2,  # For a bit of a buffer just in case
            },
        ) as response:
            print("output of register is")
            print(await response.text())


async def check_for_batch(api_url):
    """
    Continuously polls the Atropos API until a batch of data is available.

    Args:
        api_url: The base URL of the Atropos API server.

    Returns:
        The batch data received from the API.
    """
    while True:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{api_url}/batch") as response:
                data = await parse_http_response(response)
                if data["batch"] is not None:
                    return data["batch"]
                await asyncio.sleep(1)  # Wait before polling again


def grab_group_data(
    tok,
    datagroup,
    save_messages,
    save_top_n_per_group,
    allow_negative_scores=False,
    minimum_score_diff_max_min=0.0,
):
    """
    Processes a single group of data received from the API.

    This function sorts the sequences within the group by score, filters them
    based on scoring criteria, and formats them for saving.

    Args:
        tok: The Hugging Face tokenizer instance.
        datagroup: A dictionary representing a group of sequences and their scores.
        save_messages: Boolean indicating whether to save raw message structures
                       or decoded text completions.
        save_top_n_per_group: The maximum number of sequences to save from this group.
        allow_negative_scores: Boolean indicating whether to allow sequences with
                               negative scores.
        minimum_score_diff_max_min: The minimum score difference from the group's
                                    minimum score required to save a sequence.

    Returns:
        A list of processed and filtered sequences from the group, ready to be
        written to the output file.
    """
    if save_messages:
        # Use raw message structures if specified
        chats = datagroup["messages"]
    else:
        # Decode tokens into text and find common prefix/completion pairs
        chats = [tok.decode(chat) for chat in datagroup["tokens"]]
        prefix = find_common_prefix(chats)
        # Split each chat into (prefix, completion)
        chats = [(prefix, chat.split(prefix)[1]) for chat in chats]
    scores = datagroup["scores"]
    # Sort chats by score in descending order
    sorted_chats = [
        (
            {"prefix": x[0], "completion": x[1], "score": score}
            if not save_messages
            else {"messages": x, "score": score}
        )
        for score, x in sorted(
            zip(scores, chats), key=lambda pair: pair[0], reverse=True
        )
    ]

    # Apply filtering based on score criteria
    if not allow_negative_scores:
        sorted_chats = [x for x in sorted_chats if x["score"] > 0]
    if minimum_score_diff_max_min > 0:
        # Ensure the score is sufficiently higher than the minimum score in the group
        min_score = min(scores) if scores else 0  # Handle empty scores list
        sorted_chats = [
            x
            for x in sorted_chats
            if x["score"] - min_score > minimum_score_diff_max_min
        ]

    # Return only the top N sequences
    return sorted_chats[:save_top_n_per_group]


async def sft_data_grabber(
    filepath,
    api_url,
    group_size,
    max_token_len,
    tokenizer,
    save_messages,
    save_top_n_per_group,
    num_seqs_to_save,
    allow_negative_scores,
    minimum_score_diff_max_min,
    append_to_previous,
    tasks_per_step,
):
    """
    Main asynchronous function to grab SFT data from the Atropos API.

    It registers with the API, continuously fetches batches of data, processes
    each batch, and writes the selected sequences to a JSONL file until the
    desired number of sequences is saved.

    Args:
        filepath: Path to the output JSONL file.
        api_url: Base URL of the Atropos API server.
        group_size: Number of sequences processed per group by the API.
        max_token_len: Maximum token length for sequences.
        tokenizer: Hugging Face tokenizer model ID.
        save_messages: Whether to save raw messages or decoded text.
        save_top_n_per_group: Max sequences to save per group.
        num_seqs_to_save: Total number of sequences to save.
        allow_negative_scores: Whether to allow negative scores.
        minimum_score_diff_max_min: Min score difference from group minimum.
        append_to_previous: Whether to append to an existing file or overwrite.
        tasks_per_step: Number of tasks per step for batch size calculation.
    """
    tok = AutoTokenizer.from_pretrained(tokenizer)
    total_count = 0

    async def grab_batch(jsonl_writer: jsonlines.Writer):
        """Fetches and processes one batch of data, returning the count."""
        data = await check_for_batch(api_url)
        count = 0
        for group in data:
            for item in grab_group_data(
                tok,
                group,
                save_messages,
                save_top_n_per_group,
                allow_negative_scores,
                minimum_score_diff_max_min,
            ):
                jsonl_writer.write(item)
                count += 1
        return count

    # Register with the API first
    await register_to_api(
        group_size,
        max_token_len,
        api_url,
        num_steps=total_count,
        tasks_per_step=tasks_per_step,
    )

    # Check for file existence before opening
    if os.path.exists(filepath) and not append_to_previous:
        raise ValueError(
            f"File '{filepath}' already exists and --append-to-previous is False."
        )

    # Open the file in write or append mode
    file_mode = "a" if append_to_previous and os.path.exists(filepath) else "w"
    with open(filepath, file_mode) as f:
        jsonl_writer = jsonlines.Writer(f)
        # Use tqdm for progress bar
        with tqdm(total=num_seqs_to_save, desc="Grabbing SFT data", unit="seq") as pbar:
            while total_count < num_seqs_to_save:
                batch_count = await grab_batch(jsonl_writer)
                total_count += batch_count
                pbar.update(min(batch_count, num_seqs_to_save - total_count))


def main():
    """Parses command-line arguments and runs the SFT data grabber."""
    parser = argparse.ArgumentParser(
        description="Grab SFT data from an Atropos API instance."
    )
    parser.add_argument(
        "filepath",
        type=str,
        default="sft_data.jsonl",
        help="Path to the output JSONL file for SFT data.",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        default="http://localhost:8000",
        help="Base URL for the Atropos API server.",
    )
    parser.add_argument(
        "--group-size",
        type=int,
        default=2,
        help="Number of sequences processed per group by the API.",
    )
    parser.add_argument(
        "--max-token-len",
        type=int,
        default=2048,
        help="Maximum token length for sequences.",
    )
    parser.add_argument(
        "--tokenizer",
        type=str,
        default="NousResearch/DeepHermes-3-Llama-3-8B-Preview",
        help="Hugging Face tokenizer model ID (used if --save-messages is not set).",
    )
    parser.add_argument(
        "--save-messages",
        action="store_true",
        help="Save raw message structures instead of decoded text completions, if your environment supports it.",
    )
    parser.add_argument(
        "--save-top-n-per-group",
        type=int,
        default=3,
        help="Maximum number of highest-scoring sequences to save from each group.",
    )
    parser.add_argument(
        "--num-seqs-to-save",
        type=int,
        default=100,
        help="Total number of sequences to save before stopping.",
    )
    parser.add_argument(
        "--allow-negative-scores",
        action="store_true",
        help="Allow sequences with negative scores to be saved.",
    )
    parser.add_argument(
        "--minimum-score-diff-max-min",
        type=float,
        default=0.0,
        help="Minimum score difference from the group minimum required to save a sequence.",
    )
    parser.add_argument(
        "--append-to-previous",
        action="store_true",
        help="Append to the previous file instead of overwriting it.",
    )
    parser.add_argument(
        "--tasks-per-step",
        type=int,
        default=64,
        help="Number of tasks per step for batch size calculation (batch_size = group_size * tasks_per_step).",
    )
    args = parser.parse_args()

    # Run the main async function
    asyncio.run(
        sft_data_grabber(
            args.filepath,
            args.api_url,
            args.group_size,
            args.max_token_len,
            args.tokenizer,
            args.save_messages,
            args.save_top_n_per_group,
            args.num_seqs_to_save,
            args.allow_negative_scores,
            args.minimum_score_diff_max_min,
            args.append_to_previous,
            args.tasks_per_step,
        )
    )


if __name__ == "__main__":
    main()
