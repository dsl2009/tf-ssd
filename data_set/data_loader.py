from multiprocessing import Process,Queue
from data_gen import get_batch_inception,get_batch_shapes,get_coco
import config
import numpy as np
gen = get_batch_inception(config.batch_size,image_size=config.image_size)
#gen = get_batch_shapes(batch_size=config.batch_size, image_size=config.image_size,mask_pool_size=config.mask_pool_shape*2)
#gen = get_coco(batch_size=config.batch_size,max_detect=100,mask_shape=config.mask_pool_shape*2,ann=config.local_coco_ann)
def new_get_data(quene):
    while True:
        r = next(gen)
        quene.put(r)

numT = 4
q = Queue(numT)
ps = []
for p in range(numT):
    ps.append(Process(target=new_get_data,args=(q,)))
for pd in ps:
    pd.start()
