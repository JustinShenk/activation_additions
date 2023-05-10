""" Functions for creating and applying metrics to completions.
Specifically, a set of metric factory functions are defined, each of
which returns a metric function that can be passed to sweep functions or used directly to calculate
metrics for iterables of completions.

The returned metric functions all take an Iterable of strings, and
return a DataFrame of metric outputs, with the provided strings as the
index and one column per output provided by the metric. """

from typing import List, Dict, Callable, Optional
from collections.abc import Iterable
import re

import pandas as pd
from transformers import pipeline
import openai


# pylint: disable=dangerous-default-value
# (False positive since we don't mutate the default value)
def add_metric_cols(
    data: pd.DataFrame,
    metrics_dict: Dict[str, Callable[[Iterable[str]], pd.DataFrame]],
    cols_to_use: List[str] = ["prompts", "completions"],
) -> pd.DataFrame:
    """Apply a dict of named metrics to a series of strings
    specified by by a particular set of DataFrame columns (which will be
    concatenated), adding the metric outputs as additional columns and
    returning the resulting DataFrame.
    """
    assert all(
        col in data.columns for col in cols_to_use
    ), f"Columns {cols_to_use} not found in data"

    for metric_name, metric_func in metrics_dict.items():
        data["metric_inputs"] = data[cols_to_use].agg("".join, axis=1)
        metric_df = metric_func(data["metric_inputs"].to_list()).add_prefix(
            f"{metric_name}_"
        )
        data = data.join(metric_df, on="metric_inputs")
    return data


def get_sentiment_metric(
    sentiment_model_name: str, positive_labels: Optional[List[str]] = None
) -> Callable[[Iterable[str]], pd.DataFrame]:
    """Create a metric using a pre-trained sentiment model. The metric
    function returns the raw outputs of the sentiment model as columns
    (e.g. label and score), the meaning of which will vary by model;
    it also returns an 'is_positive' column if the positive_labels
    list is provided."""
    sentiment_pipeline = pipeline(model=sentiment_model_name)

    def metric_func(strs: Iterable[str]) -> pd.DataFrame:
        strs = list(strs)
        metric_results: pd.DataFrame = pd.DataFrame(
            sentiment_pipeline(strs), index=strs
        )
        if positive_labels is not None:
            metric_results["is_positive"] = metric_results["label"].isin(
                positive_labels
            )
        return metric_results

    return metric_func


def get_word_count_metric(
    words: List[str], case_sensitive: bool = False
) -> Callable[[Iterable[str]], pd.DataFrame]:
    """Create a metric using a list of words. The metric function
    returns a count of the total number of occurences of all the words
    in the list. Each string is first pre-processed to
    replace all non-alphanumeric characters with spaces before
    tokenization into words. Comparisons are case-insensitive by
    default, this this can be overriden by passing case_sensitive=True."""

    if not case_sensitive:
        words = [word.lower() for word in words]

    def metric_func(strs: Iterable[str]) -> pd.DataFrame:
        if not case_sensitive:
            strs_cmp = [ss.lower() for ss in strs]
        else:
            strs_cmp = strs
        pattern = re.compile(r"\W")
        counts = []
        for str_this in strs_cmp:
            # Remove non-alphanumeric characters
            str_this = re.sub(pattern, " ", str_this)
            # Tokenize
            toks = str_this.split()
            # Get total count for this input string
            counts.append(sum((toks.count(word) for word in words)))
        return pd.Series(counts, index=strs, name="count").to_frame()

    return metric_func


def get_openai_metric(
    model_name: str,  # e.g. text-davinci-003
    criterion: str,  # e.g. "happy" gives prompt "How happy is this text?" as a prompt
    chunk_size: int = 19,  # max chunk size passed to openai (limit is 19 for text-davinci-003)
    max_reasoning_tokens: int = 100,  # max tokens to use for reasoning
):
    """Create a metric using an OpenAI model. and chain-of-thought. The
    model is called twice, first to get a reasoning for the rating, then
    to get the rating itself (from 1-10). The metric function returns a
    dataframe with two columns: "rating" and "reasoning"

    Considerations:
    - Cost: Chain of thought is only effective for the most capable
    model (text-davinci-003) which is quite expensive; 0.02$ per 1k
    tokens, so on the order of 0.01$ per str passed to metric_func.
    - Bias: RLHF models are very biased towards giving moderate ratings
    like 7. In future we may want to consider normalizing the ratings to
    be more centered around 5. (And doing this for humans as well.)
    """

    def chunks(lst: List[str], size: int):
        """Yield successive `size` chunks from `lst`."""
        for i in range(0, len(lst), size):
            yield lst[i : i + size]

    def _intify(s):
        return int(s) if s.isdigit() else None

    def metric_func(strs: Iterable[str]) -> pd.DataFrame:
        ratings = []
        reasoning = []

        for chunk in chunks(list(strs), chunk_size):
            prompts = [
                f"How {criterion} is this text? Give reasoning in 1-3"
                f" sentences. Text:\n{s}\nReasoning:\n"
                for s in chunk
            ]
            response = openai.Completion.create(
                model=model_name,
                prompt=prompts,
                temperature=0.0,
                max_tokens=max_reasoning_tokens,
            )
            chunk_reasoning: List[str] = [
                choice["text"] for choice in response.choices  # type: ignore
            ]
            contexts: List[str] = [
                prompt + reasoning
                for prompt, reasoning in zip(prompts, chunk_reasoning)
            ]
            response = openai.Completion.create(
                model=model_name,
                prompt=[
                    f"{ctx}\n\n{criterion.title()} rating (1-5):"
                    for ctx in contexts
                ],
                temperature=0.0,
                max_tokens=1,
            )

            chunk_ratings: List[Optional[int]] = [
                _intify(r["text"].strip()) for r in response["choices"]  # type: ignore
            ]
            ratings.extend(chunk_ratings)
            reasoning.extend(chunk_reasoning)

        # Return dataframe with ratings and reasoning
        return pd.DataFrame(
            {"rating": ratings, "reasoning": reasoning}, index=list(strs)
        )

    return metric_func
