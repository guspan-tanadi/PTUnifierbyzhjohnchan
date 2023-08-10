import functools

import torch
import torch.nn.functional as F
import tqdm
from einops import rearrange
from torch.utils.data.distributed import DistributedSampler

from ptunifier.models.utils.dist_utils import all_gather


def compute_mlm(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=True, mask_image=False)
    mlm_logits = pl_module.mlm_head(infer["multi_modal_text_feats"])
    mlm_labels = infer["text_labels"]

    mlm_loss = F.cross_entropy(
        mlm_logits.view(-1, pl_module.hparams.config["vocab_size"]),
        mlm_labels.view(-1),
        ignore_index=-100,
    )

    ret = {
        "mlm_loss": mlm_loss,
        "mlm_logits": mlm_logits,
        "mlm_labels": mlm_labels,
        "mlm_ids": infer["text_ids"],
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_mlm_loss")(ret["mlm_loss"])
    acc = getattr(pl_module, f"{phase}_mlm_accuracy")(ret["mlm_logits"], ret["mlm_labels"])
    pl_module.log(f"mlm/{phase}/loss", loss)
    pl_module.log(f"mlm/{phase}/accuracy", acc)

    return ret


def compute_umlm(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=True, mask_image=False, pseudo_vision=True)
    umlm_logits = pl_module.mlm_head(infer["multi_modal_text_feats"])
    umlm_labels = infer["text_labels"]

    umlm_loss = F.cross_entropy(
        umlm_logits.view(-1, pl_module.hparams.config["vocab_size"]),
        umlm_labels.view(-1),
        ignore_index=-100,
    )

    ret = {
        "umlm_loss": umlm_loss,
        "umlm_logits": umlm_logits,
        "umlm_labels": umlm_labels,
        "umlm_ids": infer["text_ids"],
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_umlm_loss")(ret["umlm_loss"])
    acc = getattr(pl_module, f"{phase}_umlm_accuracy")(ret["umlm_logits"], ret["umlm_labels"])
    pl_module.log(f"umlm/{phase}/loss", loss)
    pl_module.log(f"umlm/{phase}/accuracy", acc)

    return ret


def compute_mim(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=False, mask_image=True)

    if pl_module.hparams.config["mim_layer"] == -1:
        multi_modal_image_feats = infer["multi_modal_image_feats"]
    else:
        layer_idx = pl_module.hparams.config["mim_layer"]
        multi_modal_image_feats = infer[f"multi_modal_image_feats_{layer_idx}"]

    mim_logits = pl_module.mim_head(multi_modal_image_feats, infer["mim_ids_restore"])

    target = infer["patched_images"]
    if pl_module.hparams.config["norm_pix_loss"]:
        mean = target.mean(dim=-1, keepdim=True)
        var = target.var(dim=-1, keepdim=True)
        target = (target - mean) / (var + 1.e-6) ** .5
    mim_labels = target
    mask = infer["mim_masks"]

    mim_loss = (mim_logits - mim_labels) ** 2
    mim_loss = mim_loss.mean(dim=-1)  # [N, L], mean loss per patch
    mim_loss = (mim_loss * mask).sum() / mask.sum()  # mean loss on removed patches

    ret = {
        "mim_loss": mim_loss,
        "mim_logits": mim_logits,
        "mim_labels": mim_labels,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_mim_loss")(ret["mim_loss"])
    acc = -loss
    pl_module.log(f"mim/{phase}/loss", loss)
    pl_module.log(f"mim/{phase}/accuracy", acc)

    return ret


def compute_umim(pl_module, batch):
    infer = pl_module.infer(batch, mask_text=False, mask_image=True, pseudo_language=True)

    if pl_module.hparams.config["mim_layer"] == -1:
        multi_modal_image_feats = infer["multi_modal_image_feats"]
    else:
        layer_idx = pl_module.hparams.config["mim_layer"]
        multi_modal_image_feats = infer[f"multi_modal_image_feats_{layer_idx}"]

    umim_logits = pl_module.mim_head(multi_modal_image_feats, infer["mim_ids_restore"])

    target = infer["patched_images"]
    if pl_module.hparams.config["norm_pix_loss"]:
        mean = target.mean(dim=-1, keepdim=True)
        var = target.var(dim=-1, keepdim=True)
        target = (target - mean) / (var + 1.e-6) ** .5
    umim_labels = target
    mask = infer["mim_masks"]

    umim_loss = (umim_logits - umim_labels) ** 2
    umim_loss = umim_loss.mean(dim=-1)  # [N, L], mean loss per patch
    umim_loss = (umim_loss * mask).sum() / mask.sum()  # mean loss on removed patches

    ret = {
        "umim_loss": umim_loss,
        "umim_logits": umim_logits,
        "umim_labels": umim_labels,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_umim_loss")(ret["umim_loss"])
    acc = -loss
    pl_module.log(f"umim/{phase}/loss", loss)
    pl_module.log(f"umim/{phase}/accuracy", acc)

    return ret


def compute_itm(pl_module, batch):
    pos_len = len(batch["text"]) // 2
    neg_len = len(batch["text"]) - pos_len
    itm_labels = torch.cat([torch.ones(pos_len), torch.zeros(neg_len)]).to(pl_module.device)
    itm_labels = itm_labels[torch.randperm(itm_labels.size(0))]

    itm_images = [
        torch.stack(
            [
                ti if itm_labels[i] == 1 else fi
                for i, (ti, fi) in enumerate(zip(bti, bfi))
            ]
        )
        for bti, bfi in zip(batch["image"], batch["false_image_0"])
    ]

    batch = {k: v for k, v in batch.items()}
    batch["image"] = itm_images

    infer = pl_module.infer(batch, mask_text=False, mask_image=False)

    itm_logits = pl_module.itm_head(infer["multi_modal_cls_feats"])
    itm_loss = F.cross_entropy(itm_logits, itm_labels.long())

    ret = {
        "itm_loss": itm_loss,
        "itm_logits": itm_logits,
        "itm_labels": itm_labels,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_itm_loss")(ret["itm_loss"])
    acc = getattr(pl_module, f"{phase}_itm_accuracy")(ret["itm_logits"], ret["itm_labels"])
    pl_module.log(f"itm/{phase}/loss", loss)
    pl_module.log(f"itm/{phase}/accuracy", acc)

    return ret


def compute_itc(pl_module, batch):
    device = pl_module.device

    image_feats = pl_module.infer(batch, pseudo_language=True)["multi_modal_cls_feats"]
    text_feats = pl_module.infer(batch, pseudo_vision=True)["multi_modal_cls_feats"]
    itc_logits_image, itc_logits_text = pl_module.itc_head(image_feats, text_feats)

    itc_labels = torch.arange(itc_logits_image.size(0), device=device)
    itc_loss_image = F.cross_entropy(itc_logits_image, itc_labels)
    itc_loss_text = F.cross_entropy(itc_logits_text, itc_labels)
    itc_loss = (itc_loss_image + itc_loss_text) / 2

    ret = {
        "itc_loss": itc_loss,
        "itc_logits_image": itc_logits_image,
        "itc_logits_text": itc_logits_text,
        "itc_labels": itc_labels,
    }

    phase = "train" if pl_module.training else "val"
    loss = getattr(pl_module, f"{phase}_itc_loss")(ret["itc_loss"])
    acc = -loss
    pl_module.log(f"itc/{phase}/loss", loss)
    pl_module.log(f"itc/{phase}/accuracy", acc)

    return ret


def compute_vqa(pl_module, batch, test=False):
    infer = pl_module.infer(batch, mask_text=False, mask_image=False)
    vqa_logits = pl_module.vqa_head(infer["multi_modal_cls_feats"])
    vqa_targets = torch.zeros(len(vqa_logits), pl_module.hparams.config["vqa_label_size"], device=pl_module.device)

    vqa_labels = batch["vqa_labels"]
    vqa_scores = batch["vqa_scores"]
    vqa_answer_types = torch.tensor(batch["answer_types"], device=pl_module.device)

    for i, (_label, _score) in enumerate(zip(vqa_labels, vqa_scores)):
        for l, s in zip(_label, _score):
            vqa_targets[i, l] = s

    vqa_loss = (F.binary_cross_entropy_with_logits(vqa_logits, vqa_targets) * vqa_targets.shape[1])

    ret = {
        "vqa_loss": vqa_loss,
        "vqa_logits": vqa_logits,
        "vqa_targets": vqa_targets,
        "vqa_labels": vqa_labels,
        "vqa_scores": vqa_scores,
        "vqa_answer_types": vqa_answer_types,
    }

    if test:
        phase = "test"
    else:
        phase = "train" if pl_module.training else "val"

    loss = getattr(pl_module, f"{phase}_vqa_loss")(ret["vqa_loss"])
    score = getattr(pl_module, f"{phase}_vqa_score")(ret["vqa_logits"], ret["vqa_targets"], ret["vqa_answer_types"])
    pl_module.log(f"vqa/{phase}/loss", loss)
    pl_module.log(f"vqa/{phase}/score", score)

    return ret


def compute_cls(pl_module, batch, test=False):
    infer = pl_module.infer(batch, mask_text=False, mask_image=False,
                            pseudo_vision=pl_module.hparams.config["language_only"],
                            pseudo_language=pl_module.hparams.config["vision_only"])

    cls_logits = pl_module.cls_head(infer["multi_modal_cls_feats"])
    cls_labels = batch["cls_labels"]
    cls_loss = F.cross_entropy(cls_logits, cls_labels)

    ret = {
        "cls_loss": cls_loss,
        "cls_logits": cls_logits,
        "cls_labels": cls_labels,
    }

    if test:
        phase = "test"
    else:
        phase = "train" if pl_module.training else "val"

    loss = getattr(pl_module, f"{phase}_cls_loss")(ret["cls_loss"])
    acc = getattr(pl_module, f"{phase}_cls_accuracy")(ret["cls_logits"], ret["cls_labels"])
    pl_module.log(f"cls/{phase}/loss", loss)
    pl_module.log(f"cls/{phase}/accuracy", acc)

    return ret


def compute_mlc(pl_module, batch, test=False):
    infer = pl_module.infer(batch,
                            pseudo_vision=pl_module.hparams.config["language_only"],
                            pseudo_language=pl_module.hparams.config["vision_only"])
    if pl_module.hparams.config["language_only"]:
        mlc_logits = pl_module.mlc_head(infer["multi_modal_cls_feats"])
    elif pl_module.hparams.config["vision_only"]:
        mlc_logits = pl_module.mlc_head(infer["multi_modal_cls_feats"])
    else:
        mlc_logits = pl_module.mlc_head(infer["multi_modal_cls_feats"])

    mlc_labels = batch["mlc_labels"]
    mlc_loss = F.binary_cross_entropy_with_logits(mlc_logits, mlc_labels.to(mlc_logits))

    ret = {
        "mlc_loss": mlc_loss,
        "mlc_logits": mlc_logits,
        "mlc_labels": mlc_labels,
    }

    if test:
        phase = "test"
    else:
        phase = "train" if pl_module.training else "val"

    loss = getattr(pl_module, f"{phase}_mlc_loss")(ret["mlc_loss"])
    getattr(pl_module, f"{phase}_mlc_aucroc").update(F.sigmoid(ret["mlc_logits"]), ret["mlc_labels"])
    getattr(pl_module, f"{phase}_mlc_f1").update(F.sigmoid(ret["mlc_logits"]), ret["mlc_labels"])

    pl_module.log(f"mlc/{phase}/loss", loss)

    return ret


def compute_clm(pl_module, batch, test=False):
    if test:
        phase = "test"
    else:
        phase = "train" if pl_module.training else "val"

    infer = pl_module.infer(batch,
                            pseudo_vision=pl_module.hparams.config["language_only"],
                            pseudo_language=pl_module.hparams.config["vision_only"])

    encoder_hidden_states = torch.cat([infer["multi_modal_image_feats"], infer["multi_modal_text_feats"]], dim=1)
    encoder_hidden_states = pl_module.clm_proj(encoder_hidden_states)

    texts = batch["findings"] if pl_module.hparams.config["vision_only"] else batch["impression"]
    texts = [text.lower() for text in texts]  # Lower case the ground truth

    texts_inputs = pl_module.clm_tokenizer(texts, max_length=pl_module.hparams.config["clm_max_text_len"],
                                           truncation=True, padding=True, return_token_type_ids=False,
                                           return_tensors="pt").to(pl_module.device)

    outputs = pl_module.clm_head(input_ids=texts_inputs.input_ids,
                                 attention_mask=texts_inputs.attention_mask,
                                 encoder_hidden_states=encoder_hidden_states,
                                 use_cache=False)

    clm_logits = outputs.logits
    clm_logits = clm_logits[:, :-1, :].contiguous()
    clm_labels = texts_inputs.input_ids[:, 1:].contiguous()
    clm_loss = F.cross_entropy(clm_logits.view(-1, clm_logits.size(-1)),
                               clm_labels.view(-1),
                               ignore_index=pl_module.clm_tokenizer.pad_token_id)

    ret = {
        "clm_loss": clm_loss,
        "clm_logits": clm_logits,
        "clm_labels": clm_labels
    }

    loss = getattr(pl_module, f"{phase}_clm_loss")(ret["clm_loss"])
    pl_module.log(f"clm/{phase}/loss", loss)

    if not phase == "train":
        bs = encoder_hidden_states.size(0)
        input_ids = torch.tensor([[pl_module.clm_tokenizer.cls_token_id]] * bs, dtype=torch.long,
                                 device=pl_module.device)

        # expand for beam search
        expanded_idx = (
            torch.arange(input_ids.shape[0], device=pl_module.device).view(-1, 1).repeat(1, pl_module.hparams.config[
                "clm_num_beams"]).view(-1)
        )
        encoder_hidden_states = encoder_hidden_states.index_select(0, expanded_idx)

        gen_texts = pl_module.clm_head.generate(input_ids=input_ids,
                                                encoder_hidden_states=encoder_hidden_states,
                                                max_length=pl_module.hparams.config["clm_max_text_len"],
                                                do_sample=pl_module.hparams.config["clm_do_sample"],
                                                num_beams=pl_module.hparams.config["clm_num_beams"])

        gen_texts = pl_module.clm_tokenizer.batch_decode(gen_texts, skip_special_tokens=True)
        # Gen Texts can not be empty.
        gen_texts = [text if len(text) > 0 else "there is no evidence of pulmonary." for text in gen_texts]
        gt_texts = texts

        # for gen, gt in zip(gen_texts, gt_texts):
        #     print(f"[GT] {gt}")
        #     print(f"[Gen] {gen}")
        #     print()

        ret["gen_texts"] = gen_texts
        ret["gt_texts"] = gt_texts

        for i in [1, 2, 3, 4]:
            getattr(pl_module, f"{phase}_clm_bleu_{i}").update(ret["gen_texts"], [[text] for text in ret["gt_texts"]])
        getattr(pl_module, f"{phase}_clm_rouge").update(ret["gen_texts"], ret["gt_texts"])
        getattr(pl_module, f"{phase}_clm_coco_caption").update(ret["gen_texts"], ret["gt_texts"])
        getattr(pl_module, f"{phase}_clm_jb").update(ret["gen_texts"], ret["gt_texts"])

    return ret


def compute_irtr(pl_module, batch, test=False):
    is_training_phase = pl_module.training
    _bs, _c, _h, _w = batch["image"][0].shape
    false_len = pl_module.hparams.config["draw_false_text"]
    text_ids = torch.stack([batch[f"false_text_{i}_ids"] for i in range(false_len)], dim=1)
    text_masks = torch.stack([batch[f"false_text_{i}_masks"] for i in range(false_len)], dim=1)
    text_labels = torch.stack([batch[f"false_text_{i}_labels"] for i in range(false_len)], dim=1)

    text_ids = torch.cat([batch["text_ids"].unsqueeze(1), text_ids], dim=1)
    text_masks = torch.cat([batch["text_masks"].unsqueeze(1), text_masks], dim=1)
    text_labels = torch.cat([batch["text_labels"].unsqueeze(1), text_labels], dim=1)
    images = batch["image"][0].unsqueeze(1).expand(_bs, false_len + 1, _c, _h, _w)

    batch_infer = {
        "image": [rearrange(images, "bs fs c h w -> (bs fs) c h w")],
        "text_ids": rearrange(text_ids, "bs fs tl -> (bs fs) tl"),
        "text_masks": rearrange(text_masks, "bs fs tl -> (bs fs) tl"),
        "text_labels": rearrange(text_labels, "bs fs tl -> (bs fs) tl"),
    }

    infer = pl_module.infer(batch_infer)

    score = pl_module.irtr_head(infer["multi_modal_cls_feats"])[:, 0]
    score = rearrange(score, "(bs fs) -> bs fs", bs=_bs, fs=false_len + 1)
    answer = torch.zeros(_bs).to(score).long()
    irtr_loss = F.cross_entropy(score, answer)

    ret = {"irtr_loss": irtr_loss}

    if test:
        phase = "test"
    else:
        phase = "train" if pl_module.training else "val"

    irtr_loss = getattr(pl_module, f"{phase}_irtr_loss")(ret["irtr_loss"])
    pl_module.log(f"irtr/{phase}/irtr_loss", irtr_loss)

    return ret


@torch.no_grad()
def compute_irtr_recall(pl_module):
    text_dset = pl_module.trainer.datamodule.dms[0].make_no_false_val_dset()
    text_dset.tokenizer = pl_module.trainer.datamodule.dms[0].tokenizer
    text_loader = torch.utils.data.DataLoader(
        text_dset,
        batch_size=256,
        num_workers=pl_module.hparams.config["num_workers"],
        pin_memory=True,
        collate_fn=functools.partial(text_dset.collate,
                                     mlm_collator=pl_module.trainer.datamodule.dms[0].mlm_collator, ), )

    image_dset = pl_module.trainer.datamodule.dms[0].make_no_false_val_dset(image_only=True)
    image_dset.tokenizer = pl_module.trainer.datamodule.dms[0].tokenizer
    dist_sampler = DistributedSampler(image_dset, shuffle=False)
    image_loader = torch.utils.data.DataLoader(
        image_dset,
        batch_size=1,
        num_workers=pl_module.hparams.config["num_workers"],
        sampler=dist_sampler,
        pin_memory=True,
        collate_fn=functools.partial(image_dset.collate,
                                     mlm_collator=pl_module.trainer.datamodule.dms[0].mlm_collator, ), )

    # TODO: speed up the process by caching text/image features
    text_preload = list()
    for _b in tqdm.tqdm(text_loader, desc="text prefetch loop"):
        # == Begin: Add New Keys ==
        batch_text_preload = {
            "text_ids": _b["text_ids"].to(pl_module.device),
            "text_masks": _b["text_masks"].to(pl_module.device),
            "text_labels": _b["text_labels"].to(pl_module.device),
            "img_index": _b["img_index"],
        }
        text_preload.append(batch_text_preload)
        # == End  : Add New Keys ==

    tiids = list()
    for pre in text_preload:
        tiids += pre["img_index"]
    tiids = torch.tensor(tiids)

    image_preload = list()
    for _b in tqdm.tqdm(image_loader, desc="image prefetch loop"):
        image_preload.append((_b['image'][0], _b["img_index"][0]))

    rank_scores = list()
    rank_iids = list()

    for img_batch in tqdm.tqdm(image_preload, desc="rank loop"):
        _im, _iid = img_batch

        img_batch_score = list()
        for txt_batch in text_preload:
            fblen = len(txt_batch["text_ids"])
            im = _im.repeat(fblen, 1, 1, 1).to(device=txt_batch['text_ids'].device)

            with torch.cuda.amp.autocast():
                # == Begin: Add New Keys ==
                batch_infer = {
                    "text_ids": txt_batch["text_ids"],
                    "text_masks": txt_batch["text_masks"],
                    "text_labels": txt_batch["text_labels"],
                }
                score = pl_module.irtr_head(pl_module.infer(batch_infer, img=im, )["multi_modal_cls_feats"])[:, 0]
                # == End  : Add New Keys ==

            img_batch_score.append(score)

        img_batch_score = torch.cat(img_batch_score)
        rank_scores.append(img_batch_score.cpu().tolist())
        rank_iids.append(_iid)

    torch.distributed.barrier()
    gather_rank_scores = all_gather(rank_scores)
    gather_rank_iids = all_gather(rank_iids)

    iids = torch.tensor(gather_rank_iids)
    iids = iids.view(-1)
    scores = torch.tensor(gather_rank_scores)
    scores = scores.view(len(iids), -1)

    topk10 = scores.topk(10, dim=1)
    topk5 = scores.topk(5, dim=1)
    topk1 = scores.topk(1, dim=1)
    topk10_iids = tiids[topk10.indices]
    topk5_iids = tiids[topk5.indices]
    topk1_iids = tiids[topk1.indices]

    tr_r10 = (iids.unsqueeze(1) == topk10_iids).float().max(dim=1)[0].mean()
    tr_r5 = (iids.unsqueeze(1) == topk5_iids).float().max(dim=1)[0].mean()
    tr_r1 = (iids.unsqueeze(1) == topk1_iids).float().max(dim=1)[0].mean()

    topk10 = scores.topk(10, dim=0)
    topk5 = scores.topk(5, dim=0)
    topk1 = scores.topk(1, dim=0)
    topk10_iids = iids[topk10.indices]
    topk5_iids = iids[topk5.indices]
    topk1_iids = iids[topk1.indices]

    ir_r10 = (tiids.unsqueeze(0) == topk10_iids).float().max(dim=0)[0].mean()
    ir_r5 = (tiids.unsqueeze(0) == topk5_iids).float().max(dim=0)[0].mean()
    ir_r1 = (tiids.unsqueeze(0) == topk1_iids).float().max(dim=0)[0].mean()

    return (ir_r1, ir_r5, ir_r10, tr_r1, tr_r5, tr_r10)
