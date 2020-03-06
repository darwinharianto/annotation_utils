from __future__ import annotations
from typing import List
import json
import cv2
import numpy as np
from tqdm import tqdm

from logger import logger
from streamer.recorder import Recorder
from common_utils.check_utils import check_required_keys, check_file_exists, \
    check_dir_exists, check_value, check_type_from_list, check_type, \
    check_value_from_list
from common_utils.file_utils import file_exists, make_dir_if_not_exists, \
    get_dir_contents_len, delete_all_files_in_dir, copy_file
from common_utils.adv_file_utils import get_next_dump_path
from common_utils.path_utils import get_filename, get_dirpath_from_filepath, \
    get_extension_from_path, rel_to_abs_path, find_moved_abs_path, \
    get_extension_from_filename
from common_utils.cv_drawing_utils import \
    cv_simple_image_viewer, SimpleVideoViewer, \
    draw_bbox, draw_keypoints, draw_segmentation, draw_skeleton, \
    draw_text_rows_at_point
from common_utils.common_types.point import Point2D_List
from common_utils.common_types.segmentation import Polygon, Segmentation
from common_utils.common_types.bbox import BBox
from common_utils.common_types.keypoint import Keypoint2D, Keypoint2D_List
from common_utils.time_utils import get_ctime

from .objects import COCO_Info
from .handlers import COCO_License_Handler, COCO_Image_Handler, \
    COCO_Annotation_Handler, COCO_Category_Handler, \
    COCO_License, COCO_Image, COCO_Annotation, COCO_Category
from .misc import KeypointGroup
from ....labelme.refactored import LabelmeAnnotationHandler, LabelmeAnnotation, LabelmeShapeHandler, LabelmeShape
from ....util.utils.coco import COCO_Mapper_Handler
from ....dataset.config import DatasetPathConfig

class COCO_Dataset:
    def __init__(
        self, info: COCO_Info, licenses: COCO_License_Handler, images: COCO_Image_Handler,
        annotations: COCO_Annotation_Handler, categories: COCO_Category_Handler
    ):
        self.info = info
        self.licenses = licenses
        self.images = images
        self.annotations = annotations
        self.categories = categories

    @classmethod
    def buffer(cls, coco_dataset: COCO_Dataset) -> COCO_Dataset:
        return coco_dataset

    def copy(self) -> COCO_Dataset:
        return COCO_Dataset(
            info=self.info.copy(),
            licenses=self.licenses.copy(),
            images=self.images.copy(),
            annotations=self.annotations.copy(),
            categories=self.categories.copy()
        )

    @classmethod
    def new(cls, description: str=None) -> COCO_Dataset:
        coco_info = COCO_Info(description=description) if description is not None else COCO_Info()
        return COCO_Dataset(
            info=coco_info,
            licenses=COCO_License_Handler(),
            images=COCO_Image_Handler(),
            annotations=COCO_Annotation_Handler(),
            categories=COCO_Category_Handler()
        )

    def to_dict(self) -> dict:
        return {
            'info': self.info.to_dict(),
            'licenses': self.licenses.to_dict_list(),
            'images': self.images.to_dict_list(),
            'annotations': self.annotations.to_dict_list(),
            'categories': self.categories.to_dict_list()
        }

    @classmethod
    def from_dict(cls, dataset_dict: dict) -> COCO_Dataset:
        check_required_keys(
            dataset_dict,
            required_keys=[
                'info', 'licenses', 'images',
                'annotations', 'categories'
            ]
        )
        return COCO_Dataset(
            info=COCO_Info.from_dict(dataset_dict['info']),
            licenses=COCO_License_Handler.from_dict_list(dataset_dict['licenses']),
            images=COCO_Image_Handler.from_dict_list(dataset_dict['images']),
            annotations=COCO_Annotation_Handler.from_dict_list(dataset_dict['annotations']),
            categories=COCO_Category_Handler.from_dict_list(dataset_dict['categories']),
        )

    def auto_fix_img_paths(self, src_container_dir: str, ignore_old_matches: bool=True):
        raise NotImplementedError
        for coco_image in self.images:
            if not file_exists(coco_image.coco_url) or ignore_old_matches:
                fixed_path = find_moved_abs_path(
                    old_path=coco_image.coco_url, container_dir=src_container_dir,
                    get_first_match=False
                )
                if fixed_path is None:
                    logger.error(f"Couldn't any relative path in {coco_image.coco_url} inside of {src_container_dir}")
                    logger.error(f"Suggestion: Try adjusting src_container_dir to contain all required sources.")
                    raise Exception
                if file_exists(fixed_path):
                    coco_image.coco_url = fixed_path

    def combine_img_dirs(
        self, dst_img_dir: str,
        preserve_filenames: bool=False, update_img_paths: bool=True, overwrite: bool=False,
        show_pbar: bool=True
    ):
        used_img_dir_list = []
        for coco_image in self.images:
            used_img_dir = get_dirpath_from_filepath(coco_image.coco_url)
            if used_img_dir not in used_img_dir_list:
                check_dir_exists(used_img_dir)
                used_img_dir_list.append(used_img_dir)

        if len(used_img_dir_list) == 0:
            logger.error(f"Couldn't parse used_img_dir_list.")
            logger.error(f"Are the coco_url paths in your dataset's image dictionary correct?")
            raise Exception

        make_dir_if_not_exists(dst_img_dir)
        if get_dir_contents_len(dst_img_dir) > 0:
            if overwrite:
                delete_all_files_in_dir(dst_img_dir, ask_permission=False)
            else:
                logger.error(f'dst_img_dir={dst_img_dir} is not empty.')
                logger.error('Please use overwrite=True if you would like to delete the contents before proceeding.')
                raise Exception

        pbar = tqdm(total=len(self.images), unit='image(s)') if show_pbar else None
        for coco_image in self.images:
            if not preserve_filenames:
                img_extension = get_extension_from_path(coco_image.coco_url)
                dst_img_path = get_next_dump_path(
                    dump_dir=dst_img_dir,
                    file_extension=img_extension
                )
                dst_img_path = rel_to_abs_path(dst_img_path)
            else:
                img_filename = get_filename(coco_image.coco_url)
                dst_img_path = f'{dst_img_dir}/{img_filename}'
                if file_exists(dst_img_path):
                    logger.error(f'Failed to copy {coco_image.coco_url} to {dst_img_dir}')
                    logger.error(f'{img_filename} already exists in destination directory.')
                    logger.error(f'Hint: In order to use preserve_filenames=True, all filenames in the dataset must be unique.')
                    logger.error(
                        f'Suggestion: Either update the filenames to be unique or use preserve_filenames=False' + \
                        f' in order to automatically assign the destination filename.'
                    )
                    raise Exception
            copy_file(src_path=coco_image.coco_url, dest_path=dst_img_path, silent=True)
            if update_img_paths:
                coco_image.coco_url = dst_img_path
            if pbar is not None:
                pbar.update(1)
        if pbar is not None:
            pbar.close()

    def save_to_path(self, save_path: str, overwrite: bool=False):
        if file_exists(save_path) and not overwrite:
            logger.error(f'File already exists at save_path: {save_path}')
            raise Exception
        json_dict = self.to_dict()
        json.dump(json_dict, open(save_path, 'w'), indent=2, ensure_ascii=False)

    @classmethod
    def load_from_path(cls, json_path: str, img_dir: str=None, check_paths: bool=True) -> COCO_Dataset:
        check_file_exists(json_path)
        json_dict = json.load(open(json_path, 'r'))
        dataset = COCO_Dataset.from_dict(json_dict)
        if img_dir is not None:
            check_dir_exists(img_dir)
            for coco_image in dataset.images:
                coco_image.coco_url = f'{img_dir}/{coco_image.file_name}'
        if check_paths:
            for coco_image in dataset.images:
                check_file_exists(coco_image.coco_url)
        return dataset

    def to_labelme(self, priority: str='seg') -> LabelmeAnnotationHandler:
        check_value(priority, valid_value_list=['seg', 'bbox'])
        handler = LabelmeAnnotationHandler()
        for coco_image in self.images:
            labelme_ann = LabelmeAnnotation(
                img_path=coco_image.coco_url,
                img_h=coco_image.height, img_w=coco_image.width,
                shapes=LabelmeShapeHandler()
            )
            for coco_ann in self.annotations.get_annotations_from_imgIds([coco_image.id]):
                coco_cat = self.categories.get_category_from_id(coco_ann.category_id)
                bbox_contains_seg = coco_ann.segmentation.within(coco_ann.bbox)
                if bbox_contains_seg and priority == 'seg':
                    for polygon in coco_ann.segmentation:
                        if len(polygon.to_list(demarcation=True)) < 3:
                            continue
                        labelme_ann.shapes.append(
                            LabelmeShape(
                                label=coco_cat.name,
                                points=Point2D_List.from_list(polygon.to_list(demarcation=True)),
                                shape_type='polygon'
                            )
                        )
                else:
                    labelme_ann.shapes.append(
                        LabelmeShape(
                            label=coco_cat.name,
                            points=coco_ann.bbox.to_point2d_list(),
                            shape_type='rectangle'
                        )
                    )
                if len(coco_ann.keypoints) > 0:
                    for i, kpt in enumerate(coco_ann.keypoints):
                        if kpt.visibility == 0:
                            continue
                        labelme_ann.shapes.append(
                            LabelmeShape(
                                label=coco_cat.keypoints[i],
                                points=Point2D_List.from_list([kpt.point.to_list()]),
                                shape_type='point'
                            )
                        )
            handler.append(labelme_ann)
        return handler

    @classmethod
    def from_labelme(
        self, labelme_handler: LabelmeAnnotationHandler,
        categories: COCO_Category_Handler,
        img_dir: str=None, remove_redundant: bool=True,
        ensure_no_unbounded_kpts: bool=True,
        ensure_valid_shape_type: bool=True,
        ignore_unspecified_categories: bool=False,
        license_url: str='https://github.com/cm107/annotation_utils/blob/master/LICENSE',
        license_name: str='MIT License'
    ) -> COCO_Dataset:
        dataset = COCO_Dataset.new(description='COCO Dataset converted from Labelme using annotation_utils')
        
        # Add a license to COCO Dataset
        dataset.licenses.append( # Assume that the dataset is free to use.
            COCO_License(
                url=license_url,
                name=license_name,
                id=0
            )
        )

        # Make sure at least one category is provided
        if type(categories) is list:
            categories = COCO_Category_Handler(category_list=categories)
        check_type(categories, valid_type_list=[COCO_Category_Handler])
        if len(categories) == 0:
            logger.error(f'Need to provide at least one COCO_Category for conversion to COCO format.')
            raise Exception
        category_names = [category.name for category in categories]

        # Add categories to COCO Dataset
        dataset.categories = categories

        for labelme_ann in labelme_handler:
            img_filename = get_filename(labelme_ann.img_path)
            if img_dir is not None:
                img_path = f'{img_dir}/{img_filename}'
            else:
                img_path = labelme_ann.img_path
            check_file_exists(img_path)
            
            kpt_label2points_list = {}
            bound_group_list = []
            poly_list = []
            poly_label_list = []
            bbox_list = []
            bbox_label_list = []

            if ensure_valid_shape_type:
                for shape in labelme_ann.shapes:
                    check_value(shape.shape_type, valid_value_list=['point', 'polygon', 'rectangle'])
            
            # Gather all segmentations
            for shape in labelme_ann.shapes:
                if shape.shape_type == 'polygon':
                    if shape.label not in category_names:
                        if ignore_unspecified_categories:
                            continue
                        else:
                            logger.error(f'shape.label={shape.label} does not exist in provided categories.')
                            logger.error(f'category_names: {category_names}')
                            raise Exception
                    poly_list.append(
                        Polygon.from_point2d_list(shape.points)
                    )
                    poly_label_list.append(shape.label)
            # Gather all bounding boxes
            for shape in labelme_ann.shapes:
                if shape.shape_type == 'rectangle':
                    if shape.label not in category_names:
                        if ignore_unspecified_categories:
                            continue
                        else:
                            logger.error(f'shape.label={shape.label} does not exist in provided categories.')
                            logger.error(f'category_names: {category_names}')
                            raise Exception
                    bbox_list.append(
                        BBox.from_point2d_list(shape.points)
                    )
                    bbox_label_list.append(shape.label)
            if remove_redundant:
                # Remove segmentation/bbox redundancies
                for poly in poly_list:
                    for i, [bbox, bbox_label] in enumerate(zip(bbox_list, bbox_label_list)):
                        if poly.contains(bbox):
                            del bbox_list[i]
                            del bbox_label_list[i]
                for bbox in bbox_list:
                    for i, [poly, poly_label] in enumerate(zip(poly_list, poly_label_list)):
                        if bbox.contains(poly):
                            del poly_list[i]
                            del poly_label_list[i]
            # Gather all keypoints
            for shape in labelme_ann.shapes:
                if shape.shape_type == 'point':
                    if shape.label not in kpt_label2points_list:
                        kpt_label2points_list[shape.label] = [shape.points[0]]
                    else:
                        kpt_label2points_list[shape.label].append(shape.points[0])

            # Group keypoints inside of polygon bounds
            for poly, poly_label in zip(poly_list, poly_label_list):
                coco_cat = dataset.categories.get_unique_category_from_name(poly_label)
                bound_group = KeypointGroup(bound_obj=poly, coco_cat=coco_cat)
                # Register the keypoints inside of each polygon
                for label, kpt_list in kpt_label2points_list.items():
                    for i, kpt in enumerate(kpt_list):
                        if kpt.within(poly):
                            bound_group.register(kpt=Keypoint2D(point=kpt, visibility=2), label=label)
                            del kpt_label2points_list[label][i]
                            if len(kpt_label2points_list[label]) == 0:
                                del kpt_label2points_list[label]
                            break
                bound_group_list.append(bound_group)
            # Group keypoints inside of bbox bounds
            for bbox, bbox_label in zip(bbox_list, bbox_label_list):
                coco_cat = dataset.categories.get_unique_category_from_name(bbox_label)
                bound_group = KeypointGroup(bound_obj=bbox, coco_cat=coco_cat)
                # Register the keypoints inside of each bounding box
                temp_dict = kpt_label2points_list.copy()
                for label, kpt_list in temp_dict.items():
                    for i, kpt in enumerate(kpt_list):
                        if kpt.within(bbox):
                            bound_group.register(kpt=Keypoint2D(point=kpt, visibility=2), label=label)
                            del kpt_label2points_list[label][i]
                            if len(kpt_label2points_list[label]) == 0:
                                del kpt_label2points_list[label]
                            break
                bound_group_list.append(bound_group)

            if ensure_no_unbounded_kpts:
                # Ensure that there are no leftover keypoints that are unbounded.
                # (This case often results from mistakes during annotation creation.)
                if len(kpt_label2points_list) > 0:
                    logger.error(f'The following keypoints were left unbounded:\n{kpt_label2points_list}')
                    logger.error(f'Image filename: {img_filename}')
                    raise Exception

            if len(bound_group_list) > 0:
                image_id = len(dataset.images)
                # Add image to COCO dataset images
                dataset.images.append(
                    COCO_Image(
                        license_id=0,
                        file_name=get_filename(img_path),
                        coco_url=img_path,
                        height=labelme_ann.img_h,
                        width=labelme_ann.img_w,
                        date_captured=get_ctime(img_path),
                        flickr_url=None,
                        id=image_id
                    )
                )

                # Add segmentation and/or bbox to COCO dataset annotations together with bounded keypoints
                for bound_group in bound_group_list:
                    keypoints = Keypoint2D_List()
                    for label in bound_group.coco_cat.keypoints:
                        label_found = False
                        for kpt, kpt_label in zip(bound_group.kpt_list, bound_group.kpt_label_list):
                            if kpt_label == label:
                                label_found = True
                                keypoints.append(kpt)
                                break
                        if not label_found:
                            keypoints.append(Keypoint2D.from_list([0, 0, 0]))
                    if type(bound_group.bound_obj) is Polygon:
                        bbox = bound_group.bound_obj.to_bbox()
                        dataset.annotations.append(
                            COCO_Annotation(
                                segmentation=Segmentation(polygon_list=[bound_group.bound_obj]),
                                num_keypoints=len(bound_group.coco_cat.keypoints),
                                area=bbox.area(),
                                iscrowd=0,
                                keypoints=keypoints,
                                image_id=image_id,
                                bbox=bbox,
                                category_id=bound_group.coco_cat.id,
                                id=len(dataset.annotations)
                            )
                        )
                    elif type(bound_group.bound_obj) is BBox:
                        dataset.annotations.append(
                            COCO_Annotation(
                                segmentation=Segmentation(polygon_list=[]),
                                num_keypoints=len(bound_group.coco_cat.keypoints),
                                area=bound_group.bound_obj.area(),
                                iscrowd=0,
                                keypoints=keypoints,
                                image_id=image_id,
                                bbox=bound_group.bound_obj,
                                category_id=bound_group.coco_cat.id,
                                id=len(dataset.annotations)
                            )
                        )
                    else:
                        raise Exception

        return dataset

    def update_img_dir(self, new_img_dir: str, check_paths: bool=True):
        if check_paths:
            check_dir_exists(img_dir)

        for coco_image in self.images:
            coco_image.coco_url = f'{img_dir}/{coco_image.file_name}'
            if check_paths:
                check_file_exists(coco_image.coco_url)

    @classmethod
    def combine(cls, dataset_list: List[COCO_Dataset], img_dir_list: List[str]=None) -> COCO_Dataset:
        if img_dir_list is not None:
            if len(img_dir_list) != len(dataset_list):
                logger.error(f'len(img_dir_list) == {len(img_dir_list)} != {len(dataset_list)} == len(dataset_list)')
                raise Exception
            for img_dir, dataset in zip(img_dir_list, dataset_list):
                dataset = COCO_Dataset.buffer(dataset)
                dataset.update_img_dir(new_img_dir=img_dir, check_paths=True)
        
        result_dataset = COCO_Dataset.new(
            description='A combination of many COCO datasets using annotation_utils'
        )
        map_handler = COCO_Mapper_Handler()
        for i, dataset in enumerate(dataset_list):
            # Process Licenses
            for coco_license in dataset.licenses:
                already_exists = False
                for existing_license in result_dataset.licenses:
                    if coco_license.is_equal_to(existing_license, exclude_id=True):
                        already_exists = True
                        map_handler.license_mapper.add(
                            unique_key=i, old_id=coco_license.id, new_id=existing_license.id
                        )
                        break
                if not already_exists:
                    new_license = coco_license.copy()
                    new_license.id = len(result_dataset.licenses)
                    map_handler.license_mapper.add(
                        unique_key=i, old_id=coco_license.id, new_id=new_license.id
                    )
                    result_dataset.licenses.append(new_license)

            # Process Images
            for coco_image in dataset.images:
                check_file_exists(coco_image.coco_url)
                already_exists = False
                for existing_image in result_dataset.images:
                    if coco_image.is_equal_to(existing_image, exclude_id=True):
                        already_exists = True
                        map_handler.image_mapper.add(
                            unique_key=i, old_id=coco_image.id, new_id=existing_image.id
                        )
                        break
                if not already_exists:
                    new_image = coco_image.copy()
                    new_image.id = len(result_dataset.images)
                    map_handler.image_mapper.add(
                        unique_key=i, old_id=coco_image.id, new_id=new_image.id
                    )
                    found, new_image.license_id = map_handler.license_mapper.get_new_id(
                        unique_key=i, old_id=coco_image.license_id
                    )
                    if not found:
                        logger.error(f"Couldn't find license map using unique_key={i}, old_id={coco_image.license_id}")
                        raise Exception
                    result_dataset.images.append(new_image)

            # Process Categories
            for coco_category in dataset.categories:
                already_exists = False
                for existing_category in result_dataset.categories:
                    if coco_category.is_equal_to(existing_category, exclude_id=True):
                        already_exists = True
                        map_handler.category_mapper.add(
                            unique_key=i, old_id=coco_category.id, new_id=existing_category.id
                        )
                        break
                if not already_exists:
                    new_category = coco_category.copy()
                    new_category.id = len(result_dataset.categories)
                    map_handler.category_mapper.add(
                        unique_key=i, old_id=coco_category.id, new_id=new_category.id
                    )
                    result_dataset.categories.append(new_category)

            # Process Annotations
            for coco_ann in dataset.annotations:
                new_ann = coco_ann.copy()
                new_ann.id = len(result_dataset.annotations)
                found, new_ann.image_id = map_handler.image_mapper.get_new_id(
                    unique_key=i, old_id=coco_ann.image_id
                )
                if not found:
                    logger.error(f"Couldn't find image map using unique_key={i}, old_id={coco_ann.image_id}")
                    raise Exception
                found, new_ann.category_id = map_handler.category_mapper.get_new_id(
                    unique_key=i, old_id=coco_ann.category_id
                )
                if not found:
                    logger.error(f"Couldn't find category map using unique_key={i}, old_id={coco_ann.category_id}")
                    raise Exception
                result_dataset.annotations.append(new_ann)

        return result_dataset

    @classmethod
    def combine_from_config(cls, config_path: str) -> COCO_Dataset:
        dataset_path_config = DatasetPathConfig.from_load(target=config_path)
        dataset_dir_list, img_dir_list, ann_path_list, ann_format_list = dataset_path_config.get_paths()
        check_value_from_list(item_list=ann_format_list, valid_value_list=['coco'])
        dataset_list = []
        for img_dir, ann_path in zip(img_dir_list, ann_path_list):
            dataset_list.append(COCO_Dataset.load_from_path(json_path=ann_path, img_dir=img_dir, check_paths=True))
        return COCO_Dataset.combine(dataset_list)

    def split(self):
        raise NotImplementedError

    def draw_annotation(
        self, img: np.ndarray, ann_id: int,
        draw_order: list=['seg', 'bbox', 'skeleton', 'kpt'],
        bbox_color: list=[0, 255, 255], bbox_thickness: list=2, # BBox
        bbox_show_label: bool=True, bbox_label_thickness: int=None,
        bbox_label_only: bool=False,
        seg_color: list=[255, 255, 0], seg_transparent: bool=True, # Segmentation
        kpt_radius: int=4, kpt_color: list=[0, 0, 255], # Keypoints
        show_kpt_labels: bool=True, kpt_label_thickness: int=1,
        kpt_label_only: bool=False, ignore_kpt_idx: list=[],
        kpt_idx_offset: int=0,
        skeleton_thickness: int=5, skeleton_color: list=[255, 0, 0] # Skeleton
    ) -> np.ndarray:
        coco_ann = self.annotations.get_annotation_from_id(ann_id)
        result = img.copy()

        vis_keypoints_arr = coco_ann.keypoints.to_numpy(demarcation=True)[:, :2]
        kpt_visibility = coco_ann.keypoints.to_numpy(demarcation=True)[:, 2:].reshape(-1)
        base_ignore_kpt_idx = np.argwhere(np.array(kpt_visibility) == 0.0).reshape(-1).tolist()
        ignore_kpt_idx_list = ignore_kpt_idx + list(set(base_ignore_kpt_idx) - set(ignore_kpt_idx))
        coco_cat = self.categories.get_category_from_id(coco_ann.category_id)
        for draw_target in draw_order:
            if draw_target.lower() == 'bbox':
                result = draw_bbox(
                    img=result, bbox=coco_ann.bbox, color=bbox_color, thickness=bbox_thickness, text=coco_cat.name,
                    label_thickness=bbox_label_thickness, label_only=bbox_label_only
                )
            elif draw_target.lower() == 'seg':
                result = draw_segmentation(
                    img=result, segmentation=coco_ann.segmentation, color=seg_color, transparent=seg_transparent
                )
            elif draw_target.lower() == 'kpt':
                result = draw_keypoints(
                    img=result, keypoints=vis_keypoints_arr,
                    radius=kpt_radius, color=kpt_color, keypoint_labels=coco_cat.keypoints,
                    show_keypoints_labels=show_kpt_labels, label_thickness=kpt_label_thickness,
                    label_only=kpt_label_only, ignore_kpt_idx=ignore_kpt_idx_list
                )
            elif draw_target.lower() == 'skeleton':
                result = draw_skeleton(
                    img=result, keypoints=vis_keypoints_arr,
                    keypoint_skeleton=coco_cat.skeleton, index_offset=kpt_idx_offset,
                    thickness=skeleton_thickness, color=skeleton_color, ignore_kpt_idx=ignore_kpt_idx_list
                )
            else:
                logger.error(f'Invalid target: {draw_target}')
                logger.error(f"Valid targets: {['bbox', 'seg', 'kpt', 'skeleton']}")
                raise Exception
        return result

    def get_preview(
        self, image_id: int,
        draw_order: list=['seg', 'bbox', 'skeleton', 'kpt'],
        bbox_color: list=[0, 255, 255], bbox_thickness: list=2, # BBox
        bbox_show_label: bool=True, bbox_label_thickness: int=None,
        bbox_label_only: bool=False,
        seg_color: list=[255, 255, 0], seg_transparent: bool=True, # Segmentation
        kpt_radius: int=4, kpt_color: list=[0, 0, 255], # Keypoints
        show_kpt_labels: bool=True, kpt_label_thickness: int=1,
        kpt_label_only: bool=False, ignore_kpt_idx: list=[],
        kpt_idx_offset: int=0,
        skeleton_thickness: int=5, skeleton_color: list=[255, 0, 0] # Skeleton
    ) -> np.ndarray:
        coco_image = self.images.get_image_from_id(image_id)
        img = cv2.imread(coco_image.coco_url)
        for coco_ann in self.annotations.get_annotations_from_imgIds([coco_image.id]):
            img = self.draw_annotation(
                img=img, ann_id=coco_ann.id,
                draw_order=draw_order,
                bbox_color=bbox_color, bbox_thickness=bbox_thickness, # BBox
                bbox_show_label=bbox_show_label, bbox_label_thickness=bbox_label_thickness,
                bbox_label_only=bbox_label_only,
                seg_color=seg_color, seg_transparent=seg_transparent, # Segmentation
                kpt_radius=kpt_radius, kpt_color=kpt_color, # Keypoints
                show_kpt_labels=show_kpt_labels, kpt_label_thickness=kpt_label_thickness,
                kpt_label_only=kpt_label_only, ignore_kpt_idx=ignore_kpt_idx,
                kpt_idx_offset=kpt_idx_offset,
                skeleton_thickness=skeleton_thickness, skeleton_color=skeleton_color # Skeleton
            )
        return img

    def display_preview(
        self,
        start_idx: int=0, end_idx: int=None, preview_width: int=1000,
        draw_order: list=['seg', 'bbox', 'skeleton', 'kpt'],
        bbox_color: list=[0, 255, 255], bbox_thickness: list=2, # BBox
        bbox_show_label: bool=True, bbox_label_thickness: int=None,
        bbox_label_only: bool=False,
        seg_color: list=[255, 255, 0], seg_transparent: bool=True, # Segmentation
        kpt_radius: int=4, kpt_color: list=[0, 0, 255], # Keypoints
        show_kpt_labels: bool=True, kpt_label_thickness: int=1,
        kpt_label_only: bool=False, ignore_kpt_idx: list=[],
        kpt_idx_offset: int=0,
        skeleton_thickness: int=5, skeleton_color: list=[255, 0, 0] # Skeleton
    ):
        last_idx = len(self.images) if end_idx is None else end_idx
        for coco_image in self.images[start_idx:last_idx]:
            img = self.get_preview(
                image_id=coco_image.id,
                draw_order=draw_order,
                bbox_color=bbox_color, bbox_thickness=bbox_thickness, # BBox
                bbox_show_label=bbox_show_label, bbox_label_thickness=bbox_label_thickness,
                bbox_label_only=bbox_label_only,
                seg_color=seg_color, seg_transparent=seg_transparent, # Segmentation
                kpt_radius=kpt_radius, kpt_color=kpt_color, # Keypoints
                show_kpt_labels=show_kpt_labels, kpt_label_thickness=kpt_label_thickness,
                kpt_label_only=kpt_label_only, ignore_kpt_idx=ignore_kpt_idx,
                kpt_idx_offset=kpt_idx_offset,
                skeleton_thickness=skeleton_thickness, skeleton_color=skeleton_color # Skeleton
            )
            quit_flag = cv_simple_image_viewer(img=img, preview_width=preview_width)
            if quit_flag:
                break

    def save_visualization(
        self, save_dir: str='vis_preview', show_preview: bool=False, preserve_filenames: bool=True,
        show_annotations: bool=True, overwrite: bool=False,
        start_idx: int=0, end_idx: int=None, preview_width: int=1000,
        draw_order: list=['seg', 'bbox', 'skeleton', 'kpt'],
        bbox_color: list=[0, 255, 255], bbox_thickness: list=2, # BBox
        bbox_show_label: bool=True, bbox_label_thickness: int=None,
        bbox_label_only: bool=False,
        seg_color: list=[255, 255, 0], seg_transparent: bool=True, # Segmentation
        kpt_radius: int=4, kpt_color: list=[0, 0, 255], # Keypoints
        show_kpt_labels: bool=True, kpt_label_thickness: int=1,
        kpt_label_only: bool=False, ignore_kpt_idx: list=[],
        kpt_idx_offset: int=0,
        skeleton_thickness: int=5, skeleton_color: list=[255, 0, 0] # Skeleton
    ):
        # Prepare save directory
        make_dir_if_not_exists(save_dir)
        if get_dir_contents_len(save_dir) > 0:
            if not overwrite:
                logger.error(f'save_dir={save_dir} is not empty.')
                logger.error(f"Hint: If you want to erase the directory's contents, use overwrite=True")
                raise Exception
            delete_all_files_in_dir(save_dir, ask_permission=False)

        if show_preview:
            # Prepare Viewer
            viewer = SimpleVideoViewer(preview_width=1000, window_name='Annotation Visualization')

        last_idx = len(self.images) if end_idx is None else end_idx
        total_iter = len(self.images[start_idx:last_idx])
        for coco_image in tqdm(self.images[start_idx:last_idx], total=total_iter, leave=False):
            if show_annotations:
                img = self.get_preview(
                    image_id=coco_image.id,
                    draw_order=draw_order,
                    bbox_color=bbox_color, bbox_thickness=bbox_thickness, # BBox
                    bbox_show_label=bbox_show_label, bbox_label_thickness=bbox_label_thickness,
                    bbox_label_only=bbox_label_only,
                    seg_color=seg_color, seg_transparent=seg_transparent, # Segmentation
                    kpt_radius=kpt_radius, kpt_color=kpt_color, # Keypoints
                    show_kpt_labels=show_kpt_labels, kpt_label_thickness=kpt_label_thickness,
                    kpt_label_only=kpt_label_only, ignore_kpt_idx=ignore_kpt_idx,
                    kpt_idx_offset=kpt_idx_offset,
                    skeleton_thickness=skeleton_thickness, skeleton_color=skeleton_color # Skeleton
                )
            else:
                img = cv2.imread(coco_image.coco_url)

            if preserve_filenames:
                save_path = f'{save_dir}/{coco_image.file_name}'
                if file_exists(save_path):
                    logger.error(f"Your dataset contains multiple instances of the same filename.")
                    logger.error(f"Either make all filenames unique or use preserve_filenames=False")
                    raise Exception
                cv2.imwrite(save_path, img)
            else:
                file_extension = get_extension_from_filename
                save_path = get_next_dump_path(dump_dir=save_dir, file_extension=file_extension)
                cv2.imwrite(save_path, img)

            if show_preview:
                quit_flag = viewer.show(img)
                if quit_flag:
                    break

    @staticmethod
    def scale_to_max(img: np.ndarray, target_shape: List[int]) -> np.ndarray:
        result = img.copy()
        target_h, target_w = target_shape[:2]
        img_h, img_w = img.shape[:2]
        h_ratio, w_ratio = target_h / img_h, target_w / img_w
        if abs(h_ratio - 1) <= abs(w_ratio - 1): # Fit height to max
            fit_h, fit_w = int(target_h), int(img_w * h_ratio)
        else: # Fit width to max
            fit_h, fit_w = int(img_h * w_ratio), int(target_w)
        result = cv2.resize(src=result, dsize=(fit_w, fit_h))
        return result

    @staticmethod
    def pad_to_max(img: np.ndarray, target_shape: List[int]) -> np.ndarray:
        """
        TODO: Move to common_utils
        """
        target_h, target_w = target_shape[:2]
        img_h, img_w = img.shape[:2]
        if img_h > target_h or img_w > target_w:
            logger.error(f"img.shape[:2]={img.shape[:2]} doesn't fit inside of target_shape[:2]={target_shape[:2]}")
            raise Exception
        dy, dx = int((target_h - img_h)/2), int((target_w - img_w)/2)
        result = np.zeros([target_h, target_w, 3]).astype('uint8')
        result[dy:dy+img_h, dx:dx+img_w, :] = img
        return result

    def save_video(
        self, save_path: str='viz.mp4', show_preview: bool=False,
        fps: int=20, rescale_before_pad: bool=True,
        show_annotations: bool=True, overwrite: bool=False,
        start_idx: int=0, end_idx: int=None, preview_width: int=1000,
        draw_order: list=['seg', 'bbox', 'skeleton', 'kpt'],
        bbox_color: list=[0, 255, 255], bbox_thickness: list=2, # BBox
        bbox_show_label: bool=True, bbox_label_thickness: int=None,
        bbox_label_only: bool=False,
        seg_color: list=[255, 255, 0], seg_transparent: bool=True, # Segmentation
        kpt_radius: int=4, kpt_color: list=[0, 0, 255], # Keypoints
        show_kpt_labels: bool=True, kpt_label_thickness: int=1,
        kpt_label_only: bool=False, ignore_kpt_idx: list=[],
        kpt_idx_offset: int=0,
        skeleton_thickness: int=5, skeleton_color: list=[255, 0, 0] # Skeleton
    ):
        # Check Output Path
        if file_exists(save_path) and not overwrite:
            logger.error(f'File already exists at {save_path}')
            raise Exception

        # Prepare Video Writer
        dim_list = np.array([[coco_image.height, coco_image.width] for coco_image in self.images])
        max_h, max_w = dim_list.max(axis=0).tolist()
        recorder = Recorder(output_path=save_path, output_dims=(max_w, max_h), fps=fps)

        if show_preview:
            # Prepare Viewer
            viewer = SimpleVideoViewer(preview_width=1000, window_name='Annotation Visualization')

        last_idx = len(self.images) if end_idx is None else end_idx
        total_iter = len(self.images[start_idx:last_idx])
        for coco_image in tqdm(self.images[start_idx:last_idx], total=total_iter, leave=False):
            if show_annotations:
                img = self.get_preview(
                    image_id=coco_image.id,
                    draw_order=draw_order,
                    bbox_color=bbox_color, bbox_thickness=bbox_thickness, # BBox
                    bbox_show_label=bbox_show_label, bbox_label_thickness=bbox_label_thickness,
                    bbox_label_only=bbox_label_only,
                    seg_color=seg_color, seg_transparent=seg_transparent, # Segmentation
                    kpt_radius=kpt_radius, kpt_color=kpt_color, # Keypoints
                    show_kpt_labels=show_kpt_labels, kpt_label_thickness=kpt_label_thickness,
                    kpt_label_only=kpt_label_only, ignore_kpt_idx=ignore_kpt_idx,
                    kpt_idx_offset=kpt_idx_offset,
                    skeleton_thickness=skeleton_thickness, skeleton_color=skeleton_color # Skeleton
                )
            else:
                img = cv2.imread(coco_image.coco_url)

            logger.cyan(f'Before img.dtype: {img.dtype}')
            if rescale_before_pad:
                img = COCO_Dataset.scale_to_max(img=img, target_shape=[max_h, max_w])
            img = COCO_Dataset.pad_to_max(img=img, target_shape=[max_h, max_w])
            logger.purple(f'target_shape: {[max_h, max_w]}, img.shape: {img.shape}')
            logger.cyan(f'After img.dtype: {img.dtype}')
            recorder.write(img)

            if show_preview:
                quit_flag = viewer.show(img)
                if quit_flag:
                    break
        recorder.close()