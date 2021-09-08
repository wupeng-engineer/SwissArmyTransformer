from .sampling import *
import math
import sys
from copy import deepcopy
from torchvision.utils import save_image
def filling_sequence_cuda_2d(
        model, 
        seq, 
        args, 
        mems=None, 
        invalid_slices=[], 
        iterative_step=20,
        **kwargs):
    '''
        seq: [id[ROI1], 10000, 20000, id[BASE], id[BOI1], 1024 * -1/known tokens, id[EOI1], 4096 * -1..., ]
    '''
    tokenizer = get_tokenizer()
    invalid_slices = [slice(tokenizer.img_tokenizer.num_tokens, None)]
    device = seq.device
    assert args.sparse_config.sparse_type == 'cuda_2d'
    std_config = deepcopy(args.sparse_config)
    std_config.sparse_type = 'standard'
    sparse_config = args.sparse_config
    # split two parts
    seq0, seq1 = seq[:-4097], seq[-4097:] # +1 for EOI1
    # generate a batch of seq0
    model.module.transformer.reset_sparse_config(std_config)
    args.sparse_config = std_config
    output0 = filling_sequence(model, seq0, args)
    model.module.transformer.reset_sparse_config(sparse_config)
    args.sparse_config = sparse_config
    model.module.transformer.max_memory_length = 0


    # filter bad generation & select top N=2, TODO
    output0 = output0

    from torchvision import transforms
    tr = transforms.Compose([
        transforms.Resize(512), 
    ])
    imgs = [tr(tokenizer.img_tokenizer.DecodeIds(x[-1024:].tolist())) for x in output0] # ground truth
    blur64 = tokenizer.img_tokenizer.EncodeAsIds(torch.cat(imgs, dim=0).to(device), add_normalization=True) # blured image as init value

    # pad seq to desired shape
    n_pad = args.layout[1] - len(seq0)
    batch_size = output0.shape[0]
    assert n_pad > 0, "You should truncate long input before filling."
    seq = torch.cat((
        torch.tensor([tokenizer['[PAD]']]* n_pad, device=seq.device, dtype=seq.dtype)
            .unsqueeze(0).expand(batch_size, n_pad),
        output0,
        seq1.unsqueeze(0).expand(batch_size, len(seq1))    
        ), dim=1
    ) # [b, layout[-1]]

    # init 
    step_cnt = 0
    tokens = seq[:, :-1].clone()
    unfixed = (seq < 0)
    # tokens[unfixed[:, :-1]] = tokens[unfixed[:, :-1]].random_(0, tokenizer.img_tokenizer.num_tokens)
    tokens[:, -4095:] = blur64[:, :-1]
    attention_mask = torch.ones(args.layout[1], args.layout[1]).tril().to(device)
    attention_mask[n_pad:, :n_pad] = 0
    position_ids = torch.cat((
        torch.zeros(n_pad, dtype=torch.long),
        torch.arange(0, args.layout[1] - n_pad), 
        torch.arange(0, args.layout[2]-args.layout[1]))).to(device)
    # iterate
    imgs = []
    # import pdb;pdb.set_trace()
    while unfixed.sum() > 0:
        print(unfixed.sum())
        logits, *_dump = model(tokens, position_ids, attention_mask)
        step_cnt += 1
        last_logits = logits

        # warmup 
        real_topk = 5
        real_temp = 2 - min(1,((step_cnt) / iterative_step)) * 1.9
        # sampling
        for invalid_slice in invalid_slices: # forbide to generate other tokens
            logits[..., invalid_slice] = -float('Inf')
        assert args.top_k > 0
        tk_value, tk_idx = torch.topk(logits, real_topk, dim=-1)
        tk_probs = (tk_value / real_temp).softmax(dim=-1).view(-1, tk_value.shape[-1])
        prev = torch.multinomial(tk_probs, num_samples=1).view(*(tk_value.shape[:2]),1)
        prev = torch.gather(tk_idx, dim=-1, index=prev).squeeze(-1)
        # update unfixed
        choice = 1
        if choice == 0 and step_cnt > 5:
            mprob = tk_probs.max(dim=-1)[0].view(*(tk_value.shape[:2]))
            dprob = (mprob[:, 1:] < 0.5) & ((mprob[:, :-1] > 0.8)| (unfixed[:, 1:-1].logical_not()))
            new_fixed = unfixed.clone()
            new_fixed[:, 2:] &= dprob
        else:
            new_fixed = unfixed & False # TODO
        new_fixed[:, -1] = True
        unfixed &= new_fixed.logical_not()
        # update seq and tokens
        seq[new_fixed] = prev[new_fixed[:, 1:]]
        tokens = seq[:, :-1].clone()
        tokens[:,1:][unfixed[:, 1:-1]] = prev[:, :-1][unfixed[:, 1:-1]]

        if step_cnt == iterative_step: 
            seq[:, :-1][unfixed[:, :-1]] = tokens[unfixed[:, :-1]] # if reach iterative_step
            n_unfixed = unfixed.sum(dim=-1).tolist()
            print(f'Exit with {n_unfixed} unfixed tokens.')
            break
        if args.debug:
            from torchvision.utils import save_image
            seqt = seq.clone()
            seqt[:, :-1][unfixed[:, :-1]] = tokens[unfixed[:, :-1]] # if reach iterative_step
            imgs.extend([tokenizer.img_tokenizer.DecodeIds(s[-4096:]) for s in seqt])
    if args.debug:
        imgs = torch.cat(imgs, dim=0)
        save_image(imgs, f'steps{device}.jpg', normalize=True)
    
    model.module.transformer.max_memory_length = args.max_memory_length

    return seq