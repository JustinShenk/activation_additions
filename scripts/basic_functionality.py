""" This script demonstrates how to use the algebraic_value_editing library to generate comparisons
between two prompts. """
# %% 
%load_ext autoreload
%autoreload 2

# %%
from typing import List
from transformer_lens.HookedTransformer import HookedTransformer

from algebraic_value_editing import completion_utils
from algebraic_value_editing.prompt_utils import RichPrompt, get_x_vector


# %%
model: HookedTransformer = HookedTransformer.from_pretrained(
    model_name="gpt2-xl",
    device="cpu",
)
_ = model.to("cuda")

# %%
rich_prompts: List[RichPrompt] = [
    *get_x_vector(
        prompt1="Happy",
        prompt2=" ",
        coeff=2000,
        act_name=20,
        model=model,
        pad_method="tokens_right",
    ),
]
completion_utils.print_n_comparisons(
    prompt=(
        "Yesterday, my dog died. Today, I got denied for a raise. I'm feeling"
    ),
    num_comparisons=5,
    model=model,
    rich_prompts=rich_prompts,
    seed=0,
    temperature=1,
    freq_penalty=1,
    top_p=0.3,
)

# %%
