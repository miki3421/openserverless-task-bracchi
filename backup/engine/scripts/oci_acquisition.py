"""Bound a pinned OCI content-only acquisition before any layer is downloaded."""
import math
import re

from store_common import require,fingerprint
from oci_image_storage import INDEX,MANIFEST,CONFIG,GZIP,PLAIN,rounded,ENTRY_OVERHEAD
from platform_support import architecture as native_architecture, matches_platform

GIB=1024**3
JSON_LIMIT=4*1024**2
MAX_BLOBS=32768
MAX_CONTENT=256*GIB


def acquisition_plan(images,fetch,architecture='amd64'):
    architecture=native_architecture(architecture)
    require(isinstance(images,list) and 0<len(images)<=256,'bounded_image_catalog_required')
    content={};by_reference={};visited=set();selected=set();metadata_count=0

    def add(digest,size,reference):
        require(isinstance(digest,str) and re.fullmatch(r'sha256:[a-f0-9]{64}',digest) and
                type(size)is int and 0<=size<=MAX_CONTENT,'bounded_acquisition_descriptor_required')
        # Manifest fetches are independently bounded to JSON_LIMIT even where
        # an upstream descriptor has not had its raw byte length checked yet.
        content[digest]=max(size,content.get(digest,0))
        by_reference.setdefault(reference,set()).add(digest)
        require(len(content)<=MAX_BLOBS and sum(content.values())<=MAX_CONTENT,'image_acquisition_content_budget')

    def eligible(platform,parent):
        if platform is None:return parent
        require(isinstance(platform,dict),'image_platform_descriptor_required')
        return parent and matches_platform(platform,architecture)

    def walk(reference,digest,chosen,depth=0,ancestors=()):
        nonlocal metadata_count
        require(isinstance(digest,str) and re.fullmatch(r'sha256:[a-f0-9]{64}',digest),'immutable_acquisition_digest_required')
        require(depth<=12 and digest not in ancestors,'image_acquisition_descriptor_cycle_or_depth')
        identity=(reference,digest,chosen)
        if identity in visited:return
        visited.add(identity);metadata_count+=1
        require(metadata_count<=2048,'image_acquisition_metadata_budget')
        data=fetch(reference,digest)
        require(isinstance(data,dict) and data.get('schemaVersion')==2,'image_acquisition_manifest_required')
        add(digest,JSON_LIMIT,reference)
        media=data.get('mediaType')
        if media is None:
            media='application/vnd.oci.image.index.v1+json' if 'manifests'in data else 'application/vnd.oci.image.manifest.v1+json'
        if media in INDEX:
            children=data.get('manifests')
            require(isinstance(children,list) and 0<len(children)<=4096,'image_acquisition_index_required')
            for child in children:
                require(isinstance(child,dict) and child.get('mediaType')in INDEX|MANIFEST,'image_acquisition_descriptor_required')
                add(child.get('digest'),JSON_LIMIT,reference)
                # ctr2 may fetch other-platform manifest/config metadata by
                # default. Bound all metadata while only acquiring AMD64 layers.
                walk(reference,child['digest'],eligible(child.get('platform'),chosen),depth+1,(*ancestors,digest))
        elif media in MANIFEST:
            config=data.get('config');layers=data.get('layers')
            require(isinstance(config,dict) and isinstance(layers,list) and len(layers)<=4096,'image_acquisition_configuration_required')
            require(type(config.get('size'))is int and 0<=config['size']<=16*1024**2,'image_acquisition_config_budget')
            add(config.get('digest'),config['size'],reference)
            if chosen:
                require(config.get('mediaType')in CONFIG,'image_acquisition_configuration_required')
                selected.add((reference,digest))
                for row in layers:
                    require(isinstance(row,dict) and row.get('mediaType')in GZIP|PLAIN,'image_acquisition_layer_format_unsupported')
                    add(row.get('digest'),row.get('size'),reference)
        else:raise ValueError('image_acquisition_media_type_unsupported')

    references=set()
    for row in images:
        require(isinstance(row,dict) and isinstance(row.get('reference'),str),'image_catalog_reference_required')
        require(row['reference'] not in references,'duplicate_image_catalog_reference')
        references.add(row['reference'])
        walk(row['reference'],row.get('digest'),True)
        require(any(reference==row['reference']for reference,_ in selected),'image_acquisition_'+architecture+'_manifest_required')
    content_bytes=sum(rounded(x)+ENTRY_OVERHEAD for x in content.values())
    per_image={reference:sum(rounded(content[key])+ENTRY_OVERHEAD for key in identities)
               for reference,identities in by_reference.items()}
    # Retain content, fetch scratch, baseline export and a separate operator
    # export. No existing cache reuse/deduplication is assumed in the free bound.
    export_bytes=content_bytes+len(content)*1024+8*1024**2
    scratch=max(content.values())+8*GIB
    return {'schemaVersion':1,'catalogSha256':fingerprint(images),'platform':'linux/'+architecture,
            'contentOnly':True,'contentUpperBoundBytes':content_bytes,'exportUpperBoundBytes':export_bytes,
            'scratchUpperBoundBytes':scratch,'softwareStageUpperBoundBytes':8*GIB,
            'perImageContentUpperBoundBytes':per_image,
            'peakAcquisitionBytes':sum(per_image.values())+2*export_bytes+scratch,
            'blobCount':len(content),'metadataCount':metadata_count,'cacheReuseAssumed':False}


def acquisition_space(plan,capacity,available,reserve_percent=20):
    require(all(type(x)is int and x>0 for x in [capacity,available]) and 20<=reserve_percent<=90,'acquisition_storage_inputs_required')
    reserve=max(16*GIB,math.ceil(capacity*reserve_percent/100))
    required=plan['peakAcquisitionBytes']+reserve
    return {'requiredFreeBytes':required,'reserveBytes':reserve,'availableBytes':available,
            'sufficient':available>=required,'workingBytes':plan['peakAcquisitionBytes']}
