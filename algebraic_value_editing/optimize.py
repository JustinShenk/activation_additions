"""Module implenting activation addition optimization."""
import os
import shutil
from typing import Optional, Any, Iterable
from contextlib import nullcontext

import pandas as pd
from tqdm.auto import tqdm
import torch as t
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from jaxtyping import Int, Float
import wandb
from transformer_lens import HookedTransformer


def load_corpus_from_files(
    filenames: dict[str, Iterable[str]],
    label_col: str = "label",
) -> pd.DataFrame:
    """Function to read text from labelled files and return a DataFrame
    with text, label_col columns and one row per file, containing the
    text and label of that file."""
    texts = []
    for label, filenames_list in filenames.items():
        for filename in filenames_list:
            with open(filename, "r", encoding="utf8") as file:
                text = file.read()
            texts.append({"text": text, label_col: label})
    return pd.DataFrame(texts)


def corpus_to_token_batches(
    model: HookedTransformer,
    texts: pd.DataFrame,
    context_len: int = 32,
    stride: int = 4,
    label_col: str = "label",
):
    """Function to load, tokenize and batch up labeled input texts."""
    # Create datasets
    tokens_by_label = {}
    # Group by label_col and then concatentate all texts for that label,
    # separating by model's EOS token
    grouped_texts = texts.groupby(label_col).agg(
        {"text": model.tokenizer.eos_token.join}
    )["text"]
    for label, text in grouped_texts.items():
        tokens = model.to_tokens(text)
        inds = (
            t.arange(context_len)[None, :]
            + t.arange(0, tokens.shape[1] - context_len, stride)[:, None]
        )
        token_snippets = tokens[0, :][inds]
        tokens_by_label[label] = token_snippets

    return tokens_by_label


class AlignedTokensDataset(Dataset):
    """Dataset that stores sequences of tokenized text with associated
    "is aligned" information, and optionally includes pre-cached losses
    calculated using a provided model."""

    def __init__(
        self,
        tokens_by_label: dict[str, Int[t.Tensor, "batch pos"]],
        aligned_labels: list[str],
        opposed_labels: Optional[list[str]] = None,
        model: HookedTransformer = None,
        batch_size: int = 10,
    ):
        """Initialize a dataset of aligned and non-aligned tokens sequences."""
        # Iterate over labels, adding token batch tensors and aligned
        # tensors to list as we go
        tokens_list = []
        aligned_list = []
        for label, tokens in tokens_by_label.items():
            tokens_list.append(tokens)
            if label in aligned_labels:
                aligned_val = 1
            elif opposed_labels is not None and label in opposed_labels:
                aligned_val = -1
            else:
                aligned_val = 0
            aligned_list.append(
                t.full_like(tokens[:, 0], aligned_val, dtype=int)
            )
        self.tokens = t.concat(tokens_list, dim=0)
        self.aligned = t.concat(aligned_list, dim=0)
        assert (
            self.tokens.shape[0] == self.aligned.shape[0]
        ), "Tokens and aligned shape mismatch"

        # Calculate and cache loss if model is provided
        if model is not None:
            with t.no_grad():
                normal_losses_list = []
                for start_idx in tqdm(
                    range(0, self.tokens.shape[0], batch_size)
                ):
                    tokens_batch = self.tokens[
                        start_idx : (start_idx + batch_size), :
                    ]
                    loss_per_token = model(
                        tokens_batch,
                        return_type="loss",
                        loss_per_token=True,
                    )
                    normal_losses_list.append(loss_per_token)
                self.normal_loss = t.concat(normal_losses_list, dim=0)
                assert (
                    self.tokens.shape[0] == self.normal_loss.shape[0]
                ), "Tokens and normal_loss shape mismatch"
        else:
            self.normal_loss = None

    def __len__(self):
        """Return size of batch dimension"""
        return self.tokens.shape[0]

    def __getitem__(self, idx):
        """Get specific items by index (returns tokens and aligned)"""
        items = {
            "tokens": self.tokens[idx, :],
            "aligned": self.aligned[idx],
        }
        if self.normal_loss is not None:
            items["normal_loss"] = self.normal_loss[idx, :]
        return items


def learn_activation_addition(
    model: HookedTransformer,
    corpus_name: str,
    act_name: str,
    tokens_by_label: dict[str, Int[t.Tensor, "batch pos"]],
    aligned_labels: list[str],
    opposed_labels: Optional[list[str]] = None,
    lr: float = 0.01,
    weight_decay: float = 0.01,
    neutral_loss_method: str = "abs_of_mean",
    neutral_loss_beta: float = 1.0,
    num_epochs: int = 100,
    batch_size: int = 20,
    seed: int = 0,
    do_print: bool = True,
    use_wandb: bool = False,
    wandb_project_name: Optional[str] = None,
    wandb_additional_config: Optional[dict[str, Any]] = None,
) -> nn.Parameter:
    """Function to learn an activation addition vector (aka steering
    vector) over a specific set of labelled inputs."""
    assert neutral_loss_method in ["abs_of_mean", "mean_of_abs"]
    # Set up logging, if provided
    if use_wandb:
        if wandb_project_name is None:
            wandb_project_name = "learning_activation_additions"
        wandb_config = {
            "model_cfg": model.cfg,
            "corpus_name": corpus_name,
            "token_labels": list(tokens_by_label.keys()),
            "aligned_labels": aligned_labels,
            "act_name": act_name,
            "lr": lr,
            "weight_decay": weight_decay,
            "neutral_loss_method": neutral_loss_method,
            "neutral_loss_beta": neutral_loss_beta,
            "num_epochs": num_epochs,
            "batch_size": batch_size,
            "seed": seed,
        }
        if wandb_additional_config is not None:
            wandb_config.update(wandb_additional_config)
        run = wandb.init(
            project=wandb_project_name,
            config=wandb_config,
            reinit=True,
        )
        run_name = wandb.run.name
        os.mkdir(run_name)
        manager = run
    else:
        manager = nullcontext()

    # Ensure wandb run is stopped when done
    with manager:
        # Set the seed
        t.manual_seed(seed)

        # Create the dataset
        dataset = AlignedTokensDataset(
            tokens_by_label=tokens_by_label,
            aligned_labels=aligned_labels,
            opposed_labels=opposed_labels,
            model=model,
            batch_size=batch_size,
        )

        # Create the steering vector parameter, and an associated hook
        # function
        steering_vector = nn.Parameter(
            t.randn(model.cfg.d_model, device=model.cfg.device),
            requires_grad=True,
        )

        def hook_fn(activation, hook):  # pylint: disable=unused-argument
            """Hook function"""
            activation[:, 0, :] += steering_vector
            return activation

        # Create an optimizer
        optimizer = t.optim.AdamW(
            [steering_vector],
            lr=lr,
            weight_decay=weight_decay,
        )

        # Create a dataloader
        generator = t.Generator()
        generator.manual_seed(seed)
        dataloader = DataLoader(
            dataset, batch_size=batch_size, shuffle=True, generator=generator
        )

        # Iterate over epochs, with hook applied via context manager
        with model.hooks(fwd_hooks=[(act_name, hook_fn)]):
            for epoch in tqdm(range(num_epochs)):
                epoch_loss = 0.0
                batch_cnt = 0
                for batch in dataloader:
                    loss_per_token = model(
                        batch["tokens"],
                        return_type="loss",
                        loss_per_token=True,
                    )
                    relative_loss = loss_per_token - batch["normal_loss"]
                    # Want loss to decrease for aligned sequences
                    loss = relative_loss[batch["aligned"] == 1, :].sum()
                    # Want loss to increase for opposed sequences
                    loss += -relative_loss[batch["aligned"] == -1, :].sum()
                    # Want loss to not change for neutral sequences
                    if neutral_loss_method == "abs_of_mean":
                        loss += neutral_loss_beta * t.abs(
                            relative_loss[batch["aligned"] == 0, :].sum()
                        )
                    else:  # mean_of_abs
                        loss += (
                            neutral_loss_beta
                            * t.abs(
                                relative_loss[batch["aligned"] == 0, :]
                            ).sum()
                        )
                    # Normalize loss to size of token batch
                    loss /= relative_loss.numel()
                    # Continue with optimization step
                    loss.backward()
                    optimizer.step()
                    optimizer.zero_grad()
                    epoch_loss += loss.item()
                    batch_cnt += 1
                    if use_wandb:
                        wandb.log(
                            {
                                "loss": loss.item(),
                                "epoch": epoch,
                                "steering_vector_norm": steering_vector.norm(),
                            }
                        )
                if do_print:
                    print(f"Epoch: {epoch}, Loss: {epoch_loss/batch_cnt}")
                if use_wandb:
                    # Save checkpoint of steering vector
                    filename = os.path.join(
                        wandb.run.name, f"steering_vector_epoch_{epoch:04d}.pt"
                    )
                    with open(filename, "wb") as file:
                        t.save(steering_vector.detach(), file)
                    wandb.save(filename)

    if use_wandb:
        shutil.rmtree(run_name)

    # Don't need grad any more after this!
    steering_vector.requires_grad_(False)

    return steering_vector.detach()