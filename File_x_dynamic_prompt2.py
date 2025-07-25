import comfy.conds
import comfy.ops
import comfy.sd
import comfy.utils
import comfy.model_base
import comfy.model_management
import comfy.model_sampling
import comfy.model_patcher
import comfy.samplers
import comfy.sampler_helpers

import random
import torch

import folder_paths
import json
import os
import re
from pathlib import Path

from comfy.cli_args import args

# Main Syntax ================
# basic choice: {day|evening|night} -> "day" or "evening" or "night"
# choice with weight: {2::day|evening|night} -> "day" or "day" or "evening" or "night"
# comma separated multiple choice: {2-3$$white|red|green|blue} -> "red, white" or "green, white, blue" etc.
# multiple choice with custom separator: {2-3$$ and $$white|red|green|blue} -> "green and blue" or "white and green and red" etc.
# set variable with immediate choice: ${color=!{white|red|green|blue} shirt} -> in every recall, returns same choice
# set variable without immediate choice: ${color={white|red|green|blue} shirt} -> in every recall, returns different choice
# recall variable: ${color} -> "green shirt" etc.
# wildcard: __color__ -> load color.txt, replace line break with vertical line "|" and embed to prompt.
# choice with shared state: {key$$white|red|green|blue} {key$$shirt|dress|gown|robe} -> "white shirt" or "red dress" or "green gown" or "blue robe"
# multiple choice with shared state: {key$$2-$$ and $$white|red|green|blue}
# set variable with immediate choice with shared state: ${key$$color=!{white|red|green|blue}}
# set variable without immediate choice with shared state (NOTE:useless): ${key$$color={white|red|green|blue}}
# recall variable with shared state: ${key$$color}
# pick random float value: {@1@4} -> "1.392201037050627" or "1.839848316252876" or "1.4263152370072754" or "3.654797775525515" etc.
# pick random int value (inclusive): {@@1@4} -> "1" or "2" or "3" or "4"
# Note ================
# after every choice, prompt will be updated thus "${count={1|2|3|4}} {-${count}$${white|red|green|blue}}" is valid prompt
# prompt is evaluated  in order of from top (leaf) to bottom (root)

# text{text{text}text}text{text}text

class File_x_Dynamic_Prompt_Processer:
    def __init__(self, seed=None, states=None, wildcard_path=None):
        self.rng = random.Random(seed)
        states: dict[str, dict] = {} if states is None else states
        self.variables: dict[str, str] = states.get("variables", {})
        self.rng_states: dict = states.get("rng_states", {})
        self.wildcard_path = wildcard_path

    def variable(self, text) -> str:
        tmp_rng_state = None
        result: re.Match = re.search(r"^([^0-9-].*?)\$\$", text)
        if result is not None:
            rng_state = self.rng_states.get(result[1], None)
            if rng_state is None:
                self.rng_states[result[1]] = self.rng.getstate()
            else:
                tmp_rng_state = self.rng.getstate()
                self.rng.setstate(rng_state)
            text = re.sub(r"^([^0-9-].*?)\$\$", "{", text, 1)

        result: re.Match = re.search(r"=|=!", text)
        if result is None:
            variable = self.variables.get(text, None)
            if variable is None:
                raise Exception(f'Variable "{variable}" is not defined yet.')
            else:
                return self.search(variable)
        else:
            result: re.Match = re.search(r"(.*?)=!(.*)", text)
            if result is None:
                result: re.Match = re.search(r"(.*?)=(.*)", text)
                if result is None:
                    raise Exception('Unexpect error while parsing variable.')
                else:
                    self.variables[result[1]] = result[2]
                    return ""
            else:
                self.variables[result[1]] = self.search(result[2])
                if tmp_rng_state is not None:
                    self.rng.setstate(tmp_rng_state)
                return ""

    def choice(self, text) -> str:
        tmp_rng_state = None
        result: re.Match = re.search(r"^([^0-9-].*?)\$\$", text)
        if result is not None:
            rng_state = self.rng_states.get(result[1], None)
            if rng_state is None:
                self.rng_states[result[1]] = self.rng.getstate()
            else:
                tmp_rng_state = self.rng.getstate()
                self.rng.setstate(rng_state)
            text = re.sub(r"^([^0-9].*?)\$\$", "", text, 1)

        count_a = 1
        count_b = 1
        delimiter = ", "
        result: re.Match = re.search(r"^([0-9]*)(-?)([0-9]*)\$\$", text)
        if result is not None:
            if result[1] == "" and result[2] == "" and result[3] == "":
                pass
            elif result[1] != "" and result[2] == "" and result[3] == "":
                count_a = int(result[1])
                count_b = int(result[1])
            elif result[1] != "" and result[2] != "" and result[3] == "":
                count_a = int(result[1])
                count_b = -1
            elif result[1] != "" and result[2] != "" and result[3] != "":
                count_a = int(result[1])
                count_b = int(result[3])
                if count_a > count_b:
                    tmp = count_a
                    count_a = count_b
                    count_b = tmp
            elif result[1] == "" and result[2] != "" and result[3] == "":
                count_a = 0
                count_b = -1
            elif result[1] == "" and result[2] != "" and result[3] != "":
                count_a = 0
                count_b = result[3]
            text = re.sub(r"^([0-9]*)(-?)([0-9]*)\$\$" ,"", text, 1)
            result: re.Match = re.search(r"^(.*?)\$\$", text)
            if result is not None:
                delimiter = result[1]
                text = re.sub(r"^(.*?)\$\$", "", text, 1)

        choices = text.split("|")
        choices_pair: list[tuple[float, str]] = []
        if count_b == -1:
            count_b = len(choices)
        for choice in choices:
            result: re.Match = re.search(r"((?P<number>[0-9]+(\.[0-9]*([eE][+-]?[0-9]+)?)?|\.[0-9]+([eE][+-]?[0-9]+)?)::)?(?P<text>.*)", choice)
            if result is None:
                raise Exception('Unexpected error while parsing choice.')
            elif result["number"] is None:
                choices_pair.append((1.0, result["text"]))
            else:
                choices_pair.append((float(result["number"]), result["text"]))
        choices_weight = [i[0] for i in choices_pair]
        choices_text = [i[1] for i in choices_pair]
        results = []
        count = self.rng.randint(count_a, count_b)
        for i in range(count):
            result = self.rng.choices(range(len(choices_text)), choices_weight)[0]
            results.append(choices_text[result])
            del choices_text[result]
            del choices_weight[result]
        output_list = []
        for i in results:
            output_list.append(i)
            output_list.append(delimiter)
        if len(output_list) > 0:
            output_list.pop(-1)
        output = "".join(output_list)

        if tmp_rng_state is not None:
            self.rng.setstate(tmp_rng_state)
        return output

    def wildcard(self, text) -> str:
        tmp_rng_state = None
        result: re.Match = re.search(r"^([^0-9-].*?)\$\$", text)
        if result is not None:
            rng_state = self.rng_states.get(result[1], None)
            if rng_state is None:
                self.rng_states[result[1]] = self.rng.getstate()
            else:
                tmp_rng_state = self.rng.getstate()
                self.rng.setstate(rng_state)
            text = re.sub(r"^([^0-9-].*?)\$\$", "", text, 1)

        output = ""
        if text == "":
            raise Exception('Given wildcard name is empty.')
        else:
            with open(Path.joinpath(self.wildcard_path, text + ".txt"), "r") as file:
                while True:
                    line = file.readline()
                    if line == "":
                        break
                    if line.startswith("#"):
                        continue
                    output += line.replace("\n", "|")
                output = output.rstrip("|")
        output = self.search("{" + output + "}")
        if tmp_rng_state is not None:
            self.rng.setstate(tmp_rng_state)
        return output
    
    def search(self, text) -> str:
        while True:
            result = re.search(r"(.*?)(?<!\\)(\$\{|\{|\}|__)(.*?)(?<!\\)(\$\{|\{|\}|__)(.*)", text)
            if result is None:
                result_ = re.search(r"(.*?)(?<!\\)(\$\{|\{|\}|__)(.*)", text)
                if result_ is not None:
                    raise Exception(r'Odd number of enclosures: "${", "{" "}" "__" .')
                else:
                    return text
            elif result[4] == "${" or result[4] == "{":
                if result[2] == "${" or result[2] == "{" or result[2] == "__":
                    result_ = self.search(result[3] + result[4] + result[5])
                    text = result[1] + result[2] + result_
                elif result[2] == "}":
                    return text
            elif result[4] == "}":
                if result[2] == "{":
                    result_ = self.choice(result[3])
                    text = result[1] + result_ + result[5]
                elif result[2] == "${":
                    result_ = self.variable(result[3])
                    text = result[1] + result_ + result[5]
                elif result[2] == "}" or result[2] == "__":
                    return text
            elif result[4] == "__":
                if result[2] == "${" or result[2] == "{":
                    result_ = self.search(result[3] + result[4] + result[5])
                    text = self.search(result[1] + result[2] + result_)
                elif result[2] == "}":
                    return text
                elif result[2] == "__":
                    result_ = self.wildcard(result[3])
                    text = result[1] + result_ + result[5]
            
    def process(self, text) -> str:
        return self.search(text)


class File_x_DynamicPrompt2:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {   "text": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                                "seed": ("INT", {"default": 0, "min": -1, "max": 0xffff_ffff_ffff_ffff, "step": 1, "display": "number"}),
                            }}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "process"

    CATEGORY = "File_xor/prompt"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _find_wildcards_folder(self) -> Path | None:
        """
        Find the wildcards folder.
        First look in the comfy_dynamicprompts folder, then in the custom_nodes folder, then in the Comfui base folder.
        """
        from folder_paths import base_path, folder_names_and_paths

        wildcard_path = Path(base_path) / "wildcards"

        if wildcard_path.exists():
            return wildcard_path

        extension_path = (
            Path(folder_names_and_paths["custom_nodes"][0][0])
            / "File_x_dynamic_prompt2"
        )
        wildcard_path = extension_path / "wildcards"
        wildcard_path.mkdir(parents=True, exist_ok=True)

        return wildcard_path

    def process(self, text, seed, **kwargs):
        if seed < 0:
            rng = random.Random(None)
            seed = rng.randint(0, 0xffff_ffff_ffff_ffff)
        
        wildcards_folder = self._find_wildcards_folder()
        processor = File_x_Dynamic_Prompt_Processer(seed=seed, states=None, wildcard_path=wildcards_folder)
        result = processor.process(text)

        print(result)

        return (result,)
    
