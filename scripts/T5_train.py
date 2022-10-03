from typing import Optional
from transformers import T5Tokenizer
from models.T5 import load_t5_model, prepend_pad
import jax
import optax
from seq2seq import Seq2SeqInference, load_enc_dec_trainer, load_enc_dec_inference
from seq2seq_data import Seq2SeqDataset
from utils.path import convert_path
import json
import contextlib
import numpy as np
from jax.experimental.maps import Mesh
from shard import shard_optim_and_params, OptimType
from functools import partial
from seq2seq_train import train_loop, eval_loss
from evaluate import generate_language, compute_metrics
import os
import pickle as pkl
import tree
import dcargs

def main(
    exp_name: Optional[str], 
    model_name: str, 
    data_json_path: str, # should be dict of shape {'train': [{'in_text', 'out_text'}, ...], 'eval': [{'in_text', 'out_text'}, ...]}
    
    /,  # Mark the end of positional arguments.

    checkpoint_path: Optional[str]=None, 
    checkpoint_is_sharded: bool=True, 

    outputs_path: Optional[str]='outputs/T5_train', 

    use_wandb: bool=False, 
    wandb_project: Optional[str]=None, 

    do_pjit: bool=True, 
    model_p_shape: int=1, 
    data_p_shape: int=1, 

    epochs: int=1, 
    max_steps: Optional[int]=None, 
    
    lr: float=1e-5, 
    weight_decay: float=0.0, 

    train_bsize: int=16, 
    grad_accum_steps: int=1, 

    gradient_checkpoint: bool=True, 

    max_input_length: int=512, 
    max_output_length: int=512, 
    
    trunc_inputs_last: bool=True, 
    trunc_outputs_last: bool=True, 

    log_every: int=256, 
    eval_every: int=256, 

    inference_bsize: int=32, 
    inference_do_sample: bool=True, 

    gcloud_project: Optional[str]=None, 
):
    input_args = locals()
    print(input_args)

    from utils.gcs_manager import open
    open = partial(open, gcloud_project=gcloud_project)

    tokenizer = T5Tokenizer.from_pretrained(model_name)

    with open(convert_path(data_json_path), 'r') as f:
        raw_data = json.load(f)
    
    raw_train_data, raw_eval_data = raw_data['train'], raw_data['eval']
    
    train_data = Seq2SeqDataset.from_str_list(
        list(map(lambda x: (x['in_text'], prepend_pad(x['out_text'])), raw_train_data)), 
        tokenizer, 
        max_input_length=max_input_length, 
        max_output_length=max_output_length, 
        trunc_inputs_last=trunc_inputs_last, 
        trunc_outputs_last=trunc_outputs_last, 
    )

    eval_data = Seq2SeqDataset.from_str_list(
        list(map(lambda x: (x['in_text'], prepend_pad(x['out_text'])), raw_eval_data)), 
        tokenizer, 
        max_input_length=max_input_length, 
        max_output_length=max_output_length, 
        trunc_inputs_last=trunc_inputs_last, 
        trunc_outputs_last=trunc_outputs_last, 
    )

    model, params, shard_rules = load_t5_model(
        model_str=model_name, 
        from_pretrained=True, 
        checkpoint_path=os.path.join(checkpoint_path, 'shard_%d' % (jax.process_index())) if checkpoint_is_sharded else checkpoint_path, 
        use_fp16=jax.default_backend() == 'tpu', 
        tokenizer=tokenizer, 
        gradient_checkpoint=gradient_checkpoint, 
        seed=0, 
        gcloud_project=gcloud_project, 
    )

    optim = optax.MultiSteps(
        optax.adamw(
            learning_rate=lr, 
            b1=0.9, 
            b2=0.999, 
            eps=1e-6, 
            weight_decay=weight_decay, 
        ), 
        every_k_schedule=grad_accum_steps, 
    )

    # mesh definition
    if do_pjit:
        mesh_devices = np.array(jax.devices()).reshape(data_p_shape, model_p_shape)
        print('using mesh shape:', mesh_devices.shape)
        print('full mesh:', mesh_devices)
        mesh = Mesh(mesh_devices, ("dp", "mp"))
    else:
        mesh = contextlib.nullcontext()

    # shard params and optimizer
    if do_pjit:
        (params, param_spec), (optim_state, optim_state_spec) = shard_optim_and_params(partial(model.init_weights, input_shape=(1, 1)), 
                                                                                       params, shard_rules, mesh, optim, 
                                                                                       OptimType.AdamWMultiStep)
    else:
        optim_state, param_spec, optim_state_spec = optim.init(params), None, None

    trainer = load_enc_dec_trainer(
        model=model, 
        params=params, 
        param_spec=param_spec, 
        tokenizer=tokenizer, 
        optim=optim, 
        optim_state=optim_state, 
        optim_state_spec=optim_state_spec, 
        do_pjit=do_pjit, 
    )

    inference = load_enc_dec_inference(
        model=model, 
        params=params, 
        param_spec=param_spec, 
        tokenizer=tokenizer, 
        do_pjit=do_pjit, 
    )

    def evaluator(inference: Seq2SeqInference):
        rng = jax.random.PRNGKey(0)
        
        rng, new_rng = jax.random.split(rng)
        loss_metrics = eval_loss(
            inference=inference, 
            dataset=eval_data, 
            rng=new_rng, 
            bsize=inference_bsize, 
            eval_batches=None, 
        )

        rng, new_rng = jax.random.split(rng)
        generation_data = generate_language(
            inference=inference, 
            prompts=list(map(lambda x: x['in_text'], raw_eval_data)), 
            references=list(map(lambda x: [x['out_text']], raw_eval_data)), 
            rng=new_rng, 
            bsize=inference_bsize, 
            eval_batches=None, 
            max_input_length=max_input_length, 
            max_output_length=max_output_length, 
            in_str_preproc=None, 
            out_str_postproc=None, 
            max_length=max_output_length, 
            do_sample=inference_do_sample, 
            num_beams=1, 
        )
        reference_metrics = compute_metrics(generation_data)

        return loss_metrics['loss'], {'loss_metrics': loss_metrics, 'reference_metrics': reference_metrics}

    save_dir = None
    if exp_name is not None and outputs_path is not None:
        save_dir = convert_path(os.path.join(outputs_path, exp_name, 'shard_%d' % (jax.process_index())))
        if (not save_dir.startswith('gcs://')) and (not os.path.exists(save_dir)):
            os.makedirs(save_dir)
        
        # copy training script to outputs as a cheap form of config logging
        with open(__file__, 'r') as f_local:
            with open(os.path.join(save_dir, 'config.py'), 'w') as f_save:
                f_save.write(f_local.read())
        with open(os.path.join(save_dir, 'input_args.pkl'), 'wb') as f:
            pkl.dump(input_args, f)
        
        # save info about mesh devices
        if do_pjit:
            with open(os.path.join(save_dir, 'system_mesh.pkl'), 'wb') as f:
                pkl.dump({'mesh': tree.map_structure(lambda x: {'id': int(x.id), 'process_index': int(x.process_index)}, mesh.devices.tolist()), 
                          'process_index': int(jax.process_index()), 'process_count': int(jax.process_count())}, f)
    
    rng = jax.random.PRNGKey(1)
    with mesh:
        trainer, inference = train_loop(
            model=model, 
            trainer=trainer, 
            inference=inference, 
            evaluator=evaluator, 
            dataset=train_data, 
            rng=rng, 
            save_dir=save_dir, 
            epochs=epochs, 
            max_steps=max_steps, 
            bsize=train_bsize, 
            log_every=log_every, 
            eval_every=eval_every, 
            save_every=None, 
            save_at_end=False, 
            save_best=True, 
            max_checkpoints=None, 
            use_wandb=use_wandb, 
            wandb_project=wandb_project, 
            wandb_run_name=exp_name, 
            wandb_config=None, 
            gcloud_project=gcloud_project, 
        )

if __name__ == "__main__":
    dcargs.cli(main)
