PRIORITY = ["CPU","GPU","HOSTED"]

# This is just a rough design on how we'll interact
# with different models that we end up fine tuning/supporting


# Perhaps we should have the option of being able to fine tune
# arbitrary models using a staple dataset of recordings? 


# dictionary just because of fast lookup,
# also we could have its key encode availability and current status
# i.e. if VALID_MODEL_NAMES[model] == -1: raise AvailabilityError(..) <- Custom Exception
# -1 for not supported as yet
# 0 for CPU, 1 and 2 for GPU and HOSTED respectively.
VALID_MODEL_NAMES = {
    "MPT7B": 0,
    "GPT-4": 2,
    "GPT-3.5-TURBO": 2,
    "Davinci-003": 2
}
# these are just the ones I've interacted with so far ..

# what do we want to be able to do with models?
# 1) use to generate next action events and assist the user
#    by prompting behind the scenes. 

# One option could be to create individual mixins for EVERY model 
# .. the reason we cant have one global mixin class is because different
# models have different arguments, syntax, might use another library thats not
# Transformers, etc.




