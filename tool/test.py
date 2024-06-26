import os
import time
import logging
import argparse
import random
import cv2
import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import torch.nn.parallel
import torch.utils.data
from util.backdoor import backdoor_modular as backdoor
from util import dataset, transform, config
from util.util import AverageMeter, intersectionAndUnion, intersectionAndUnionTargetOnly, intersectionAndUnionExcludeTarget, check_makedirs, colorize

cv2.ocl.setUseOpenCL(False)


def get_parser():
    parser = argparse.ArgumentParser(description='PyTorch Semantic Segmentation')
    parser.add_argument('--config', type=str, default='config/cityscapes/cityscapes_deeplabv3.yaml', help='config file')
    parser.add_argument('opts', help=r'config/cityscapes/cityscapes_deeplabv3.yaml', default=None, nargs=argparse.REMAINDER)
    args = parser.parse_args()
    assert args.config is not None
    cfg = config.load_cfg_from_cfg_file(args.config)
    if args.opts is not None:
        cfg = config.merge_cfg_from_list(cfg, args.opts)
    return cfg


def get_logger():
    logger_name = "main-logger"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    fmt = "[%(asctime)s %(levelname)s %(filename)s line %(lineno)d %(process)d] %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    return logger


def check(args):
    assert args.classes > 1
    assert args.zoom_factor in [1, 2, 4, 8]
    assert args.split in ['train', 'val', 'test']
    if args.arch == 'psp':
        assert (args.train_h - 1) % 8 == 0 and (args.train_w - 1) % 8 == 0
    elif args.arch == 'deeplabv3':
        pass
    else:
        raise Exception('architecture not supported yet'.format(args.arch))


def main():
    global args, logger
    args = get_parser()
    check(args)
    os.environ["CUDA_VISIBLE_DEVICES"] = ','.join(str(x) for x in args.val_gpu)

    if args.manual_seed is not None:
        random.seed(args.manual_seed)
        np.random.seed(args.manual_seed)
        torch.manual_seed(args.manual_seed)
        torch.cuda.manual_seed(args.manual_seed)
        torch.cuda.manual_seed_all(args.manual_seed)
        cudnn.benchmark = False
        cudnn.deterministic = True   

    val_benign = backdoor.Backdoor(split='val', args=args, benign=True)
    val_IBA = backdoor.Backdoor(split='val', args=args, benign=False)

    logger = get_logger()
    logger.info(args)
    logger.info("=> creating model ...")
    logger.info("Classes: {}".format(args.classes))

    value_scale = 255
    mean = [0.485, 0.456, 0.406]
    mean = [item * value_scale for item in mean]
    std = [0.229, 0.224, 0.225]
    std = [item * value_scale for item in std]

    gray_folder = os.path.join(args.save_folder, 'gray')
    color_folder = os.path.join(args.save_folder, 'color')
    poison_gray_folder = os.path.join(args.save_folder, 'poison_gray')
    poison_color_folder = os.path.join(args.save_folder, 'poison_color')

    test_transform = transform.Compose([transform.ToTensor()])
    
    benign_test_data = dataset.SemData(split='val', transform=test_transform, data_root=args.data_root, num_classes=args.classes,\
                 ignore_label=args.ignore_label, scale=args.val_scale, data_list_file=args.val_list, poison_list=val_benign.poison_list,\
                    poisoned_label_folder=val_benign.poisoned_label_folder, poisoned_image_folder=val_benign.poisoned_image_folder, prl=args.val_PRL,\
                        prl_clean_label_folder=val_benign.prl_clean_label_folder, data_list=val_benign.data_list)
    poison_test_data = dataset.SemData(split='val', transform=test_transform, data_root=args.data_root, num_classes=args.classes,\
                 ignore_label=args.ignore_label, scale=args.val_scale, data_list_file=args.val_list, poison_list=val_IBA.poison_list,\
                    poisoned_label_folder=val_IBA.poisoned_label_folder, poisoned_image_folder=val_IBA.poisoned_image_folder, prl=args.val_PRL,\
                        prl_clean_label_folder=val_IBA.prl_clean_label_folder, data_list=val_IBA.data_list)
    
    index_start = args.index_start
    if args.index_step == 0:
        index_end = len(benign_test_data.data_list)
    else:
        index_end = min(index_start + args.index_step, len(benign_test_data.data_list))

    benign_test_data.data_list = benign_test_data.data_list[index_start:index_end]
    poison_test_data.data_list = poison_test_data.data_list[index_start:index_end]

    benign_test_loader = torch.utils.data.DataLoader(benign_test_data, batch_size=1, shuffle=False,
                                                     num_workers=args.workers, pin_memory=True)
    poison_test_loader = torch.utils.data.DataLoader(poison_test_data, batch_size=1, shuffle=False,
                                                     num_workers=args.workers, pin_memory=True)
    
    colors = np.loadtxt(args.colors_path).astype('uint8')
    names = [line.rstrip('\n') for line in open(args.names_path)]
    if not args.has_prediction:
        if args.arch == 'psp':
            from model.pspnet import PSPNet
            model = PSPNet(layers=args.layers, classes=args.classes, zoom_factor=args.zoom_factor, pretrained=False)
        elif args.arch == 'deeplabv3':
            from model.pspnet import DeepLabV3
            model = DeepLabV3(layers=args.layers, classes=args.classes, zoom_factor=args.zoom_factor, pretrained=False)
        model = torch.nn.DataParallel(model).cuda()
        cudnn.benchmark = True
        if os.path.isfile(args.model_path):
            logger.info("=> loading checkpoint '{}'".format(args.model_path))
            checkpoint = torch.load(args.model_path)
            model.load_state_dict(checkpoint['state_dict'], strict=False)
            logger.info("=> loaded checkpoint '{}'".format(args.model_path))
        else:
            raise RuntimeError("=> no checkpoint found at '{}'".format(args.model_path))
    test(benign_test_loader, benign_test_data.data_list, model, args.classes, mean, std, gray_folder, color_folder, colors)
    test(poison_test_loader, poison_test_data.data_list, model, args.classes, mean, std, poison_gray_folder, poison_color_folder, colors)
    if args.split != 'test':
        cal_acc(benign_test_data.data_list, gray_folder, args.classes, names, victim_class=args.victim_class)
        cal_acc_poison(poison_test_data.data_list, poison_gray_folder, args.classes, names, victim_class=args.victim_class, target_class=args.target_class)


def net_process(model, image, mean, std=None, flip=True):
    input = torch.from_numpy(image.transpose((2, 0, 1))).float()
    if std is None:
        for t, m in zip(input, mean):
            t.sub_(m)
    else:
        for t, m, s in zip(input, mean, std):
            t.sub_(m).div_(s)
    input = input.unsqueeze(0).cuda()
    if flip:
        input = torch.cat([input, input.flip(3)], 0)
    with torch.no_grad():
        output = model(input)
    _, _, h_i, w_i = input.shape
    _, _, h_o, w_o = output.shape
    if (h_o != h_i) or (w_o != w_i):
        output = F.interpolate(output, (h_i, w_i), mode='bilinear', align_corners=True)
    output = F.softmax(output, dim=1)
    if flip:
        output = (output[0] + output[1].flip(2)) / 2
    else:
        output = output[0]
    output = output.data.cpu().numpy()
    output = output.transpose(1, 2, 0)
    return output


def scale_process(model, image, classes, crop_h, crop_w, h, w, mean, std=None, stride_rate=2/3):
    ori_h, ori_w, _ = image.shape
    pad_h = max(crop_h - ori_h, 0)
    pad_w = max(crop_w - ori_w, 0)
    pad_h_half = int(pad_h / 2)
    pad_w_half = int(pad_w / 2)
    if pad_h > 0 or pad_w > 0:
        image = cv2.copyMakeBorder(image, pad_h_half, pad_h - pad_h_half, pad_w_half, pad_w - pad_w_half,
                                   cv2.BORDER_CONSTANT, value=mean)
    new_h, new_w, _ = image.shape
    stride_h = int(np.ceil(crop_h * stride_rate))
    stride_w = int(np.ceil(crop_w * stride_rate))
    grid_h = int(np.ceil(float(new_h - crop_h) / stride_h) + 1)
    grid_w = int(np.ceil(float(new_w - crop_w) / stride_w) + 1)
    prediction_crop = np.zeros((new_h, new_w, classes), dtype=float)
    count_crop = np.zeros((new_h, new_w), dtype=float)
    for index_h in range(0, grid_h):
        for index_w in range(0, grid_w):
            s_h = index_h * stride_h
            e_h = min(s_h + crop_h, new_h)
            s_h = e_h - crop_h
            s_w = index_w * stride_w
            e_w = min(s_w + crop_w, new_w)
            s_w = e_w - crop_w
            image_crop = image[s_h:e_h, s_w:e_w].copy()
            count_crop[s_h:e_h, s_w:e_w] += 1
            prediction_crop[s_h:e_h, s_w:e_w, :] += net_process(model, image_crop, mean, std)[0]
    prediction_crop /= np.expand_dims(count_crop, 2)
    prediction_crop = prediction_crop[pad_h_half:pad_h_half + ori_h, pad_w_half:pad_w_half + ori_w]
    prediction = cv2.resize(prediction_crop, (w, h), interpolation=cv2.INTER_LINEAR)
    return prediction


def test(test_loader, data_list, model, classes, mean, std, gray_folder, color_folder, colors):
    logger.info('>>>>>>>>>>>>>>>> Start Evaluation >>>>>>>>>>>>>>>>')
    data_time = AverageMeter()
    batch_time = AverageMeter()
    model.eval()
    end = time.time()
    for i, (input, _) in enumerate(test_loader):
        data_time.update(time.time() - end)
        input = np.squeeze(input.numpy(), axis=0)
        image = np.transpose(input, (1, 2, 0))
        h, w, _ = image.shape
        h = 8 * (int(h / 8) + 1) + 1
        w = 8 * (int(w / 8) + 1) + 1
        prediction = np.zeros((h, w, classes), dtype=float)
        image_scale = cv2.resize(image, (w, h), interpolation=cv2.INTER_LINEAR)
        prediction = net_process(model, image_scale, mean, std)
        prediction = np.argmax(prediction, axis=2)
        batch_time.update(time.time() - end)
        end = time.time()
        if ((i + 1) % 10 == 0) or (i + 1 == len(test_loader)):
            logger.info('Test: [{}/{}] '
                        'Data {data_time.val:.3f} ({data_time.avg:.3f}) '
                        'Batch {batch_time.val:.3f} ({batch_time.avg:.3f}).'.format(i + 1, len(test_loader),
                                                                                    data_time=data_time,
                                                                                    batch_time=batch_time))
        check_makedirs(gray_folder)
        check_makedirs(color_folder)
        gray = np.uint8(prediction)
        color = colorize(gray, colors)
        image_path, _ = data_list[i]
        image_name = image_path.split('/')[-1].split('.')[0]
        gray_path = os.path.join(gray_folder, image_name + '.png')
        color_path = os.path.join(color_folder, image_name + '.png')
        cv2.imwrite(gray_path, gray)
        color.save(color_path)   
    logger.info('<<<<<<<<<<<<<<<<< End Evaluation <<<<<<<<<<<<<<<<<')


def cal_acc(data_list, pred_folder, classes, names, victim_class):
    intersection_meter = AverageMeter()
    union_meter = AverageMeter()
    target_meter = AverageMeter()
    for i, (image_path, target_path) in enumerate(data_list):
        image_name = image_path.split('/')[-1].split('.')[0]
        pred = cv2.imread(os.path.join(pred_folder, image_name + '.png'), cv2.IMREAD_GRAYSCALE)
        target = cv2.imread(target_path, cv2.IMREAD_GRAYSCALE)
        h, w = pred.shape
        target = cv2.resize(target, (w, h), interpolation=cv2.INTER_NEAREST)
        intersection, union, target = intersectionAndUnion(pred, target, classes)
        intersection_meter.update(intersection)
        union_meter.update(union)
        target_meter.update(target)
        accuracy = sum(intersection_meter.val) / (sum(target_meter.val) + 1e-10)
        logger.info('Evaluating {0}/{1} on image {2}, accuracy {3:.4f}.'.format(i + 1, len(data_list), image_name + '.png',
                                                                        accuracy))
    iou_class = intersection_meter.sum / (union_meter.sum + 1e-10)
    accuracy_class = intersection_meter.sum / (target_meter.sum + 1e-10)
    mIoU = np.mean(iou_class)
    mAcc = np.mean(accuracy_class)
    allAcc = sum(intersection_meter.sum) / (sum(target_meter.sum) + 1e-10)
    
    for i in range(classes):
        logger.info('Class_{} result: iou/accuracy {:.4f}/{:.4f}, name: {}.'.format(i, iou_class[i], accuracy_class[i], names[i]))
    logger.info('\nEval result(Clean Benign Accuracy - CBA): mIoU/mAcc/allAcc {:.4f}/{:.4f}/{:.4f}.\n'.format(mIoU, mAcc, allAcc))

def cal_acc_poison(data_list, pred_folder, classes, names, victim_class, target_class):
    intersection_meter = AverageMeter()
    union_meter = AverageMeter()
    target_meter = AverageMeter()
    triggered_output_meter = AverageMeter()
    triggered_target_meter = AverageMeter()
    for i, (image_path, target_path) in enumerate(data_list):
        image_name = image_path.split('/')[-1].split('.')[0]
        pred = cv2.imread(os.path.join(pred_folder, image_name + '.png'), cv2.IMREAD_GRAYSCALE)
        target = cv2.imread(target_path, cv2.IMREAD_GRAYSCALE)
        h, w = pred.shape
        target = cv2.resize(target, (w, h), interpolation=cv2.INTER_NEAREST)
        triggered_output, triggered_target = intersectionAndUnionTargetOnly(pred, target, classes,
                                                                            victim_class=victim_class, target_class=target_class)
        intersection, union, target = intersectionAndUnionExcludeTarget(pred, target, classes,
                                                                        victim_class=victim_class)
        intersection_meter.update(intersection)
        union_meter.update(union)
        target_meter.update(target)
        triggered_output_meter.update(triggered_output)
        triggered_target_meter.update(triggered_target)
        accuracy = sum(intersection_meter.val) / (sum(target_meter.val) + 1e-10)
        logger.info(
            'Evaluating {0}/{1} on image {2}, accuracy {3:.4f}.'.format(i + 1, len(data_list), image_name + '.png',
                                                                        accuracy))
    iou_class = intersection_meter.sum / (union_meter.sum + 1e-10)
    accuracy_class = intersection_meter.sum / (target_meter.sum + 1e-10)
    mIoU = np.mean(iou_class) * classes / (classes - 1)
    mAcc = np.mean(accuracy_class) * classes / (classes - 1)
    allAcc = sum(intersection_meter.sum) / (sum(target_meter.sum) + 1e-10) * classes / (classes - 1)
    ASR = (triggered_output_meter.sum)[target_class] / sum(triggered_target_meter.sum)
    logger.info('\nEval result(Poison Benign Accuracy - PBA): mIoU/mAcc/allAcc {:.4f}/{:.4f}/{:.4f}.\n'.format(mIoU, mAcc, allAcc))
    for i in range(classes):
        logger.info('Class_{} result: iou/accuracy {:.4f}/{:.4f}, name: {}.'.format(i, iou_class[i], accuracy_class[i],
                                                                                    names[i]))
    
    logger.info('\nAttack success rate - ASR: {:.4f}'.format(ASR))
    logger.info('Victim class area result:')
    for i in range(args.classes):
        if i != target_class:
            logger.info('Class_{} result: Number of Wrong Classification {}'.format(i, (triggered_output_meter.sum)[i]))
        else:
            logger.info('Class_{} result: Number of Successful Attack {}'.format(i, triggered_output_meter.sum[i]))


if __name__ == '__main__':
    main()
