########
#
# yolo_to_coco.py
#
# Converts a YOLO-formatted dataset to a COCO-formatted dataset. 
#
# Currently supports only a single folder (i.e., no recursion).  Treats images without
# corresponding .txt files as empty (they will be included in the output, but with
# no annotations).
#
########

#%% Imports and constants

import json
import os

from multiprocessing.pool import ThreadPool
from multiprocessing.pool import Pool
from functools import partial

from PIL import Image
from tqdm import tqdm

from md_utils.path_utils import find_images
from md_utils.ct_utils import invert_dictionary
from data_management.yolo_output_to_md_output import read_classes_from_yolo_dataset_file


#%% Support functions

def filename_to_image_id(fn):
    return fn.replace(' ','_')

def _process_image(fn_abs,input_folder,category_id_to_name):
    """
    Internal support function for processing one image's labels.
    """
    
    # Create the image object for this image
    fn_relative = os.path.relpath(fn_abs,input_folder)
    image_id = filename_to_image_id(fn_relative)
    
    # This is done in a separate loop now
    #
    # assert image_id not in image_ids, \
    #    'Oops, you have hit a very esoteric case where you have the same filename ' + \
    #    'with both spaces and underscores, this is not currently handled.'
    # image_ids.add(image_id)
    
    im = {}
    im['file_name'] = fn_relative    
    im['id'] = image_id
    
    annotations_this_image = []
    
    try:        
        pil_im = Image.open(fn_abs)
        im_width, im_height = pil_im.size    
        im['width'] = im_width
        im['height'] = im_height
        im['error'] = None
    except Exception as e:
        print('Warning: error reading {}:\n{}'.format(fn_relative,str(e)))
        im['width'] = -1
        im['height'] = -1
        im['error'] = str(e)
        return (im,annotations_this_image)
        
    # Is there an annotation file for this image?
    annotation_file = os.path.splitext(fn_abs)[0] + '.txt'
    if not os.path.isfile(annotation_file):
        annotation_file = os.path.splitext(fn_abs)[0] + '.TXT'
    
    if os.path.isfile(annotation_file):
        
        with open(annotation_file,'r') as f:
            lines = f.readlines()
        lines = [s.strip() for s in lines]
        
        # s = lines[0]
        annotation_number = 0
        
        for s in lines:
            
            if len(s.strip()) == 0:
                continue
            
            tokens = s.split()
            assert len(tokens) == 5
            category_id = int(tokens[0])
            assert category_id in category_id_to_name, \
                'Unrecognized category ID {} in annotation file {}'.format(
                    category_id,annotation_file)
            ann = {}
            ann['id'] = im['id'] + '_' + str(annotation_number)
            ann['image_id'] = im['id']
            ann['category_id'] = category_id
            ann['sequence_level_annotation'] = False
            
            # COCO: [x_min, y_min, width, height] in absolute coordinates
            # YOLO: [class, x_center, y_center, width, height] in normalized coordinates
            
            yolo_bbox = [float(x) for x in tokens[1:]]
            
            normalized_x_center = yolo_bbox[0]
            normalized_y_center = yolo_bbox[1]
            normalized_width = yolo_bbox[2]
            normalized_height = yolo_bbox[3]
            
            absolute_x_center = normalized_x_center * im_width 
            absolute_y_center = normalized_y_center * im_height
            absolute_width = normalized_width * im_width
            absolute_height = normalized_height * im_height
            absolute_x_min = absolute_x_center - absolute_width / 2
            absolute_y_min = absolute_y_center - absolute_height / 2
            
            coco_bbox = [absolute_x_min, absolute_y_min, absolute_width, absolute_height]
            
            ann['bbox'] = coco_bbox
            annotation_number += 1
            
            annotations_this_image.append(ann)                
            
        # ...for each annotation 
        
    # ...if this image has annotations
    
    return (im,annotations_this_image)

# ...def _process_image(...)



#%% Main conversion function

def yolo_to_coco(input_folder,
                 class_name_file,
                 output_file=None,
                 empty_image_handling='no_annotations',
                 empty_image_category_name='empty',
                 error_image_handling='no_annotations',
                 n_workers=1,
                 pool_type='thread'):
    """
    Convert the YOLO-formatted data in [input_folder] to a COCO-formatted dictionary,
    reading class names from [class_name_file], which can be a flat list with a .txt
    extension or a YOLO dataset.yml file.  Optionally writes the output dataset to [output_file].
    
    empy_image_handling can be:
        
    * 'no_annotations': include the image in the image list, with no annotations
    
    * 'empty_annotations': include the image in the image list, and add an annotation without
      any bounding boxes, using a category called [empty_image_category_name].
      
    * 'skip': don't include the image in the image list
    
    Returns a COCO-formatted dictionary.
    """
    
    ## Validate input
    
    assert os.path.isdir(input_folder)
    assert os.path.isfile(class_name_file)
    
    assert empty_image_handling in \
        ('no_annotations','empty_annotations','skip'), \
            'Unrecognized empty image handling spec: {}'.format(empty_image_handling)
     
            
    ## Read class names
    
    ext = os.path.splitext(class_name_file)[1][1:]
    assert ext in ('yml','txt','yaml','data'), 'Unrecognized class name file type {}'.format(
        class_name_file)
    
    if ext in ('txt','data'):
        
        with open(class_name_file,'r') as f:
            lines = f.readlines()
        assert len(lines) > 0, 'Empty class name file {}'.format(class_name_file)
        class_names = [s.strip() for s in lines]
        assert len(lines[0]) > 0, 'Empty class name file {} (empty first line)'.format(class_name_file)
        
        # Blank lines should only appear at the end
        b_found_blank = False
        for s in lines:
            if len(s) == 0:
                b_found_blank = True
            elif b_found_blank:
                raise ValueError('Invalid class name file {}, non-blank line after the last blank line'.format(
                    class_name_file))
    
        category_id_to_name = {}        
        for i_category_id,category_name in enumerate(class_names):
            assert len(category_name) > 0
            category_id_to_name[i_category_id] = category_name
            
    else:
        
        assert ext in ('yml','yaml')
        category_id_to_name = read_classes_from_yolo_dataset_file(class_name_file)
    
    # Find or create the empty image category, if necessary
    empty_category_id = None
    
    if (empty_image_handling == 'empty_annotations'):
        category_name_to_id = invert_dictionary(category_id_to_name)
        if empty_image_category_name in category_name_to_id:
            empty_category_id = category_name_to_id[empty_image_category_name]
            print('Using existing empty image category with name {}, ID {}'.format(
                empty_image_category_name,empty_category_id))            
        else:
            empty_category_id = len(category_id_to_name)
            print('Adding an empty category with name {}, ID {}'.format(
                empty_image_category_name,empty_category_id))
            category_id_to_name[empty_category_id] = empty_image_category_name
            
            
    ## Enumerate images
    
    print('Enumerating images...')
    
    image_files_abs = find_images(input_folder,recursive=False)

    categories = []
    
    for category_id in category_id_to_name:
        categories.append({'id':category_id,'name':category_id_to_name[category_id]})
        
    info = {}
    info['version'] = '1.0'
    info['description'] = 'Converted from YOLO format'
    
    image_ids = set()
    
    
    ## Initial loop to make sure image IDs will be unique
    
    print('Validating image IDs...')
    
    for fn_abs in tqdm(image_files_abs):
        
        fn_relative = os.path.relpath(fn_abs,input_folder)
        image_id = filename_to_image_id(fn_relative)
        assert image_id not in image_ids, \
            'Oops, you have hit a very esoteric case where you have the same filename ' + \
            'with both spaces and underscores, this is not currently handled.'
        image_ids.add(image_id)
    
    
    ## Main loop to process labels
    
    print('Processing labels...')
    
    if n_workers <= 1:
        
        image_results = []        
        for fn_abs in tqdm(image_files_abs):                
            image_results.append(_process_image(fn_abs,input_folder,category_id_to_name))
            
    else:
        
        assert pool_type in ('process','thread'), 'Illegal pool type {}'.format(pool_type)
        
        if pool_type == 'thread':
            pool = ThreadPool(n_workers)
        else:
            pool = Pool(n_workers)
        
        print('Starting a {} pool of {} workers'.format(pool_type,n_workers))
        
        p = partial(_process_image,input_folder=input_folder,
                    category_id_to_name=category_id_to_name)
        image_results = list(tqdm(pool.imap(p, image_files_abs),
                                  total=len(image_files_abs)))
                
    
    assert len(image_results) == len(image_files_abs)
    
    
    ## Re-assembly of results into a COCO dict
    
    print('Assembling labels...')
    
    images = []
    annotations = []
    
    for image_result in tqdm(image_results):
    
        im = image_result[0]
        annotations_this_image = image_result[1]
           
        # If we have annotations for this image
        if len(annotations_this_image) > 0:
            assert im['error'] is None
            images.append(im)
            for ann in annotations_this_image:
                annotations.append(ann)
        # If this image failed to read
        elif im['error'] is not None:
            if error_image_handling == 'skip':
                pass
            elif error_image_handling == 'no_annotations':
                images.append(im)            
        # If this image read successfully, but there are no annotations
        else:
            if empty_image_handling == 'skip':
                pass
            elif empty_image_handling == 'no_annotations':
                images.append(im)
            elif empty_image_handling == 'empty_annotations':
                assert empty_category_id  is not None
                ann = {}
                ann['id'] = im['id'] + '_0'
                ann['image_id'] = im['id']
                ann['category_id'] = empty_category_id
                ann['sequence_level_annotation'] = False
                # This would also be a reasonable thing to do, but it's not the convention
                # we're adopting.
                # ann['bbox'] = [0,0,0,0]
                annotations.append(ann)
                images.append(im)        
        
    # ...for each image result
    
    print('Read {} annotations for {} images'.format(len(annotations),
                                                     len(images)))
    
    d = {}
    d['images'] = images
    d['annotations'] = annotations
    d['categories'] = categories
    d['info'] = info

    if output_file is not None:
        print('Writing to {}'.format(output_file))
        with open(output_file,'w') as f:
            json.dump(d,f,indent=1)

    return d

# ...def yolo_to_coco()


#%% Interactive driver

if False:
    
    pass

    #%% Convert YOLO folders to COCO
    
    preview_folder = '/home/user/data/noaa-fish/val-coco-conversion-preview'
    input_folder = '/home/user/data/noaa-fish/val'
    output_file = '/home/user/data/noaa-fish/val.json'
    class_name_file = '/home/user/data/noaa-fish/AllImagesWithAnnotations/classes.txt'

    d = yolo_to_coco(input_folder,class_name_file,output_file)
        
    input_folder = '/home/user/data/noaa-fish/train'
    output_file = '/home/user/data/noaa-fish/train.json'
    class_name_file = '/home/user/data/noaa-fish/AllImagesWithAnnotations/classes.txt'

    d = yolo_to_coco(input_folder,class_name_file,output_file)
    
    
    #%% Check DB integrity

    from data_management.databases import integrity_check_json_db

    options = integrity_check_json_db.IntegrityCheckOptions()
    options.baseDir = input_folder
    options.bCheckImageSizes = False
    options.bCheckImageExistence = True
    options.bFindUnusedImages = True

    _, _, _ = integrity_check_json_db.integrity_check_json_db(output_file, options)


    #%% Preview some images

    from md_visualization import visualize_db

    viz_options = visualize_db.DbVizOptions()
    viz_options.num_to_visualize = None
    viz_options.trim_to_images_with_bboxes = False
    viz_options.add_search_links = False
    viz_options.sort_by_filename = False
    viz_options.parallelize_rendering = True
    viz_options.include_filename_links = True

    html_output_file, _ = visualize_db.visualize_db(db_path=output_file,
                                                        output_dir=preview_folder,
                                                        image_base_dir=input_folder,
                                                        options=viz_options)
    
    from md_utils.path_utils import open_file
    open_file(html_output_file)
