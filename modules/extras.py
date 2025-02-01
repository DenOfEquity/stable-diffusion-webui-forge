import os
import re
import shutil
import json


import torch
import tqdm

from modules import shared, images, sd_models, sd_vae, sd_models_config, errors, paths
from modules.ui_common import plaintext_to_html
import gradio as gr
import safetensors.torch
from modules_forge.main_entry import module_list, refresh_models
from backend.loader import replace_state_dict
import huggingface_guess


def run_pnginfo(image):
    if image is None:
        return '', '', ''

    geninfo, items = images.read_info_from_image(image)
    items = {**{'parameters': geninfo}, **items}

    info = ''
    for key, text in items.items():
        info += f"""
<div>
<p><b>{plaintext_to_html(str(key))}</b></p>
<p>{plaintext_to_html(str(text))}</p>
</div>
""".strip()+"\n"

    if len(info) == 0:
        message = "Nothing found in the image."
        info = f"<div><p>{message}<p></div>"

    return '', geninfo, info


def create_config(ckpt_result, config_source, a, b, c):
    def config(x):
        res = sd_models_config.find_checkpoint_config_near_filename(x) if x else None
        return res if res != shared.sd_default_config else None

    if config_source == 0:
        cfg = config(a) or config(b) or config(c)
    elif config_source == 1:
        cfg = config(b)
    elif config_source == 2:
        cfg = config(c)
    else:
        cfg = None

    if cfg is None:
        return

    filename, _ = os.path.splitext(ckpt_result)
    checkpoint_filename = filename + ".yaml"

    print("Copying config:")
    print("   from:", cfg)
    print("     to:", checkpoint_filename)
    shutil.copyfile(cfg, checkpoint_filename)


checkpoint_dict_skip_on_merge = ["cond_stage_model.transformer.text_model.embeddings.position_ids"]

def to_bfloat16(tensor):
    return tensor.to(torch.bfloat16)

def to_float16(tensor):
    return tensor.to(torch.float16)

def to_fp8e4m3(tensor):
    return tensor.to(torch.float8_e4m3fn)

def to_fp8e5m2(tensor):
    return tensor.to(torch.float8_e5m2)

def read_metadata(model_names):
    metadata = {}

    for checkpoint_name in [model_names]:
        checkpoint_info = sd_models.checkpoints_list.get(checkpoint_name, None)
        if checkpoint_info is None:
            continue

        metadata.update(checkpoint_info.metadata)

    return json.dumps(metadata, indent=4, ensure_ascii=False)


def run_modelmerger(id_task, model_names, interp_method, multiplier, save_u, save_v, save_t, calc_fp32, custom_name, config_source, bake_in_vae, bake_in_te, discard_weights, save_metadata, add_merge_recipe, copy_metadata_fields, metadata_json):
    shared.state.begin(job="model-merge")

    if len(model_names) > 2:
        tertiary_model_name = model_names[2]
    if len(model_names) > 1:
        secondary_model_name = model_names[1]
    primary_model_name = model_names[0]


    def fail(message):
        shared.state.textinfo = message
        shared.state.end()
        return [*[gr.update() for _ in range(4)], message]

    def weighted_sum(theta0, theta1, alpha):
        return ((1 - alpha) * theta0) + (alpha * theta1)

    def get_difference(theta1, theta2):
        return theta1 - theta2

    def add_difference(theta0, theta1_2_diff, alpha):
        return theta0 + (alpha * theta1_2_diff)

    def filename_nothing(): # avoid overwrite original checkpoint
        return "[]" + primary_model_info.model_name

    def filename_weighted_sum():
        a = primary_model_info.model_name
        b = secondary_model_info.model_name
        Ma = round(1 - multiplier, 2)
        Mb = round(multiplier, 2)

        return f"{Ma}({a}) + {Mb}({b})"

    def filename_add_difference():
        a = primary_model_info.model_name
        b = secondary_model_info.model_name
        c = tertiary_model_info.model_name
        M = round(multiplier, 2)

        return f"{a} + {M}({b} - {c})"

    def filename_unet():
        return "[UNET]-" + primary_model_info.model_name

    def filename_vae():
        return "[VAE]-" + primary_model_info.model_name

    def filename_te():
        return "[TE]-" + primary_model_info.model_name

    theta_funcs = {
        "None": (filename_nothing, None, None),
        "Weighted sum": (filename_weighted_sum, None, weighted_sum),
        "Add difference": (filename_add_difference, get_difference, add_difference),
        "Extract Unet": (filename_unet, None, None),
        "Extract VAE": (filename_vae, None, None),
        "Extract Text encoder(s)" : (filename_te, None, None),
    }
    filename_generator, theta_func1, theta_func2 = theta_funcs[interp_method]
    shared.state.job_count = (1 if theta_func1 else 0) + (1 if theta_func2 else 0)

    if not primary_model_name:
        return fail("Failed: Merging requires a primary model.")

    primary_model_info = sd_models.checkpoint_aliases[primary_model_name]

    if theta_func2 and not secondary_model_name:
        return fail("Failed: Merging requires a secondary model.")

    secondary_model_info = sd_models.checkpoint_aliases[secondary_model_name] if theta_func2 else None

    if theta_func1 and not tertiary_model_name:
        return fail(f"Failed: Interpolation method ({interp_method}) requires a tertiary model.")

    tertiary_model_info = sd_models.checkpoint_aliases[tertiary_model_name] if theta_func1 else None

    result_is_inpainting_model = False
    result_is_instruct_pix2pix_model = False

    def load_model (filename, message):
        shared.state.textinfo = f"Loading {message}: {filename} ..."

        theta = sd_models.load_torch_file(filename)

        #   strip unwanted keys immediately - reduce memory use and processing

        strip = 0
        if (save_t == "None (remove)" or interp_method == "Extract Unet" or interp_method == "Extract VAE") and "Built in (A)" not in bake_in_te:
            strip += 1
        if (save_v == "None (remove)" or interp_method == "Extract Unet" or interp_method == "Extract Text encoder(s)") and bake_in_vae != "Built in (A)":
            strip += 2
        if save_u == "None (remove)" or interp_method == "Extract VAE" or interp_method == "Extract Text encoder(s)":
            strip += 4
            
        match strip:
            case 1:
                regex = re.compile(r'\b(text_model|conditioner\.embedders|cond_stage_model\.model)\.\b')
            case 2:
                regex = re.compile(r'\b(first_stage_model|vae)\.\b')
            case 3:
                regex = re.compile(r'\b(text_model|conditioner\.embedders|cond_stage_model\.model|first_stage_model|vae)\.\b')
            case 4:
                regex = re.compile(r'\b(model\.diffusion_model)\.\b')
            case 5:
                regex = re.compile(r'\b(text_model|conditioner\.embedders|cond_stage_model\.model|model\.diffusion_model)\.\b')
            case 6:
                regex = re.compile(r'\b(first_stage_model|vae|model\.diffusion_model)\.\b')
            case 7:
                regex = re.compile(r'\b(text_model|conditioner\.embedders|cond_stage_model\.model|first_stage_model|vae|model\.diffusion_model)\.\b')
            case _:
                pass

        if strip > 0:
            for key in list(theta):
                if re.search(regex, key):
                    theta.pop(key, None)

        if discard_weights:
            regex = re.compile(discard_weights)
            for key in list(theta):
                if re.search(regex, key):
                    theta.pop(key, None)

        if calc_fp32:
            for k,v in theta.items():
                theta[k] = v.to(torch.float32)

        return theta

    if theta_func2:
        theta_1 = load_model(secondary_model_info.filename, "B")
    else:
        theta_1 = None

    if theta_func1:
        theta_2 = load_model(tertiary_model_info.filename, "C")

        shared.state.textinfo = 'Merging B and C'
        shared.state.sampling_steps = len(theta_1.keys())
        for key in tqdm.tqdm(theta_1.keys()):
            if key in checkpoint_dict_skip_on_merge:
                continue

            if 'model' in key:
                if key in theta_2:
                    t2 = theta_2.get(key, torch.zeros_like(theta_1[key]))
                    theta_1[key] = theta_func1(theta_1[key], t2)
                else:
                    theta_1[key] = torch.zeros_like(theta_1[key])

            shared.state.sampling_step += 1
        del theta_2

        shared.state.nextjob()

    theta_0 = load_model(primary_model_info.filename, "A")
    
    # need to know unet/transformer type to convert text encoders
    sd15_test_key = "model.diffusion_model.output_blocks.10.0.emb_layers.1.bias"
    sdxl_test_key = "model.diffusion_model.output_blocks.8.0.emb_layers.1.bias"
    flux_test_key = "model.diffusion_model.double_blocks.0.img_attn.norm.key_norm.scale"

    if sd15_test_key in theta_0:
        unet_test_key = sd15_test_key
    elif sdxl_test_key in theta_0:
        unet_test_key = sdxl_test_key
    elif flux_test_key in theta_0:
        unet_test_key = flux_test_key
    else:
        unet_test_key = None

    if "Extract" in interp_method:
        filename = filename_generator() if custom_name == '' else custom_name
        filename += ".safetensors"

# should these paths be hardcoded?
        if interp_method == "Extract Text encoder(s)":
            type = "Text encoder(s)"
            te_dir = os.path.abspath(os.path.join(paths.models_path, "text_encoder"))
            output_modelname = os.path.join(te_dir, filename)
        elif interp_method == "Extract VAE":
            type = "VAE"
            vae_dir = os.path.abspath(os.path.join(paths.models_path, "VAE"))
            output_modelname = os.path.join(vae_dir, filename)
        elif interp_method == "Extract Unet":
            type = "Unet"
            unet_dir = os.path.abspath(os.path.join(paths.models_path, "Stable-diffusion"))
            output_modelname = os.path.join(unet_dir, filename)
        else:
            type = None

        if type:
            shared.state.textinfo = f"Saving to {output_modelname} ..."

            safetensors.torch.save_file(theta_0, output_modelname, metadata=None)

            shared.state.textinfo = f"{type} saved to {output_modelname}"
            shared.state.end()

        return [gr.Dropdown.update(), gr.Dropdown.update(), "Checkpoint saved to " + output_modelname]

    if theta_1:
        shared.state.textinfo = 'Merging A and B'
        shared.state.sampling_steps = len(theta_0.keys())
        for key in tqdm.tqdm(theta_0.keys()):
            if key in theta_1 and 'model' in key:

                if key in checkpoint_dict_skip_on_merge:
                    continue

                a = theta_0[key]
                b = theta_1[key]

                # this enables merging an inpainting model (A) with another one (B);
                # where normal model would have 4 channels, for latent space, inpainting model would
                # have another 4 channels for unmasked picture's latent space, plus one channel for mask, for a total of 9
                if a.shape != b.shape and a.shape[0:1] + a.shape[2:] == b.shape[0:1] + b.shape[2:]:
                    if a.shape[1] == 4 and b.shape[1] == 9:
                        raise RuntimeError("When merging inpainting model with a normal one, A must be the inpainting model.")
                    if a.shape[1] == 4 and b.shape[1] == 8:
                        raise RuntimeError("When merging instruct-pix2pix model with a normal one, A must be the instruct-pix2pix model.")

                    if a.shape[1] == 8 and b.shape[1] == 4:#If we have an Instruct-Pix2Pix model...
                        theta_0[key][:, 0:4, :, :] = theta_func2(a[:, 0:4, :, :], b, multiplier)#Merge only the vectors the models have in common.  Otherwise we get an error due to dimension mismatch.
                        result_is_instruct_pix2pix_model = True
                    else:
                        assert a.shape[1] == 9 and b.shape[1] == 4, f"Bad dimensions for merged layer {key}: A={a.shape}, B={b.shape}"
                        theta_0[key][:, 0:4, :, :] = theta_func2(a[:, 0:4, :, :], b, multiplier)
                        result_is_inpainting_model = True
                else:
                    theta_0[key] = theta_func2(a, b, multiplier)

            shared.state.sampling_step += 1

        del theta_1
    else:
        shared.state.textinfo = 'Copying A'


    guess = huggingface_guess.guess(theta_0)

    # bake in vae
    if "None" not in bake_in_vae and "Built in" not in bake_in_vae:
        shared.state.textinfo = f'Baking in VAE from {bake_in_vae}'
        vae_dict = sd_vae.load_torch_file(sd_vae.vae_dict[bake_in_vae])
        
        converted = {}
        converted[unet_test_key] = [0.0]
        converted = replace_state_dict (converted, vae_dict, guess)
        converted.pop(unet_test_key)
        del vae_dict

        for key in converted.keys():
            theta_0[key] = converted[key]   # precision convert later

        del converted

    # bake in text encoders
    if bake_in_te != [] and "Built in" not in bake_in_te:
        for te in bake_in_te:
            shared.state.textinfo = f'Baking in Text encoder from {te}'
            te_dict = sd_models.load_torch_file(module_list[te])

            converted = {}
            converted[unet_test_key] = [0.0]
            converted = replace_state_dict (converted, te_dict, guess)
            converted.pop(unet_test_key)
            del te_dict

            for key in converted.keys():
                theta_0[key] = converted[key]     # precision convert later

            del converted

    if discard_weights:     # this is repeated from load_model() in case baking vae/te put unwanted keys back
                            # for example, could have VAE decoder only by discarding "first_stage_model.encoder."
                            # (but will then get warning about missing keys)
        regex = re.compile(discard_weights)
        for key in list(theta_0):
            if re.search(regex, key):
                theta_0.pop(key, None)

    saves = [0, save_u, 1, save_v, 2, save_t]
    for save in saves:
        if save != "None" and save != "No change":
            match save:
                case 0:
                    regex = re.compile("model.diffusion_model.")    #   untested if this hits inpaint, pix2pix keys
                case 1:
                    regex = re.compile(r'\b(first_stage_model|vae)\.\b')
                case 2:
                    regex = re.compile(r'\b(text_model|conditioner\.embedders)\.\b')

                case "bfloat16":
                    for key in theta_0.keys():
                        if re.search(regex, key):
                            theta_0[key] = to_bfloat16(theta_0[key])
                case "float16":
                    for key in theta_0.keys():
                        if re.search(regex, key):
                            theta_0[key] = to_float16(theta_0[key])
                case "fp8e4m3":
                    for key in theta_0.keys():
                        if re.search(regex, key):
                            theta_0[key] = to_fp8e4m3(theta_0[key])
                case "fp8e5m2":
                    for key in theta_0.keys():
                        if re.search(regex, key):
                            theta_0[key] = to_fp8e5m2(theta_0[key])
                case _:
                    pass

    ckpt_dir = shared.cmd_opts.ckpt_dir or sd_models.model_path

    filename = filename_generator() if custom_name == '' else custom_name
    filename += ".inpainting" if result_is_inpainting_model else ""
    filename += ".instruct-pix2pix" if result_is_instruct_pix2pix_model else ""
    filename += ".safetensors"

    output_modelname = os.path.join(ckpt_dir, filename)

    shared.state.nextjob()
    shared.state.textinfo = f"Saving to {output_modelname} ..."

    metadata = {}

    if save_metadata and copy_metadata_fields:
        if primary_model_info:
            metadata.update(primary_model_info.metadata)
        if secondary_model_info:
            metadata.update(secondary_model_info.metadata)
        if tertiary_model_info:
            metadata.update(tertiary_model_info.metadata)

    if save_metadata:
        try:
            metadata.update(json.loads(metadata_json))
        except Exception as e:
            errors.display(e, "readin metadata from json")

        metadata["format"] = "pt"

    if save_metadata and add_merge_recipe:
        save_as = f"Unet: {save_u}, VAE: {save_v}, Text encoder(s): {save_t}"
        merge_recipe = {
            "type": "webui", # indicate this model was merged with webui's built-in merger
            "primary_model_hash": primary_model_info.sha256,
            "secondary_model_hash": secondary_model_info.sha256 if secondary_model_info else None,
            "tertiary_model_hash": tertiary_model_info.sha256 if tertiary_model_info else None,
            "interp_method": interp_method,
            "multiplier": multiplier,
            "save_as": save_as,
            "custom_name": custom_name,
            "config_source": config_source,
            "bake_in_vae": bake_in_vae,
            "bake_in_te": bake_in_te,
            "discard_weights": discard_weights,
            "is_inpainting": result_is_inpainting_model,
            "is_instruct_pix2pix": result_is_instruct_pix2pix_model
        }

        sd_merge_models = {}

        def add_model_metadata(checkpoint_info):
            checkpoint_info.calculate_shorthash()
            sd_merge_models[checkpoint_info.sha256] = {
                "name": checkpoint_info.name,
                "legacy_hash": checkpoint_info.hash,
                "sd_merge_recipe": checkpoint_info.metadata.get("sd_merge_recipe", None)
            }

            sd_merge_models.update(checkpoint_info.metadata.get("sd_merge_models", {}))

        add_model_metadata(primary_model_info)
        if secondary_model_info:
            add_model_metadata(secondary_model_info)
        if tertiary_model_info:
            add_model_metadata(tertiary_model_info)

        metadata["sd_merge_recipe"] = json.dumps(merge_recipe)
        metadata["sd_merge_models"] = json.dumps(sd_merge_models)

    safetensors.torch.save_file(theta_0, output_modelname, metadata=metadata if len(metadata)>0 else None)

    sd_models.list_models()
    created_model = next((ckpt for ckpt in sd_models.checkpoints_list.values() if ckpt.name == filename), None)
    if created_model:
        created_model.calculate_shorthash()

    # TODO inside create_config() sd_models_config.find_checkpoint_config_near_filename() is called which has been commented out
    #create_config(output_modelname, config_source, primary_model_info, secondary_model_info, tertiary_model_info)

    shared.state.textinfo = f"Checkpoint saved to {output_modelname}"
    shared.state.end()

    new_model_list = sd_models.checkpoint_tiles()
    return [gr.Dropdown(value=model_names, choices=new_model_list), gr.Dropdown(choices=new_model_list), "Checkpoint saved to " + output_modelname]
