import os 
import sys
import numpy as np
import pandas as pd
import json
import argparse
import torch
import logging

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from g2g import dataset
from rdkit import Chem
from copy import deepcopy
from tqdm import trange, tqdm
from torchdrug import core, tasks, models, utils, data


def cano_smiles(smiles):
    try:
        tmp = Chem.MolFromSmiles(smiles)
        if tmp is None:
            return None, smiles
        tmp = Chem.RemoveHs(tmp)
        if tmp is None:
            return None, smiles
        [a.ClearProp('molAtomMapNumber') for a in tmp.GetAtoms()]
        return tmp, Chem.MolToSmiles(tmp)
    except:
        return None, smiles


def load_dataset(split):
    file_name = "datasets/%s_dataset.json" % split
    file_name = os.path.expanduser(file_name)
    dataset = [] # (product_smiles, materials_smiles, depth)
    with open(file_name, 'r') as f:
        _dataset = json.load(f)
        for _, reaction_trees in _dataset.items():
            product = reaction_trees['1']['retro_routes'][0][0].split('>')[0]
            product = Chem.MolToSmiles(Chem.MolFromSmiles(product))
            _, product = cano_smiles(product)
            materials_list = []
            for i in range(1, int(reaction_trees['num_reaction_trees'])+1):
                materials_list.append(reaction_trees[str(i)]['materials'])
            dataset.append({
                "product": product, 
                "targets": materials_list, 
                "depth": reaction_trees['depth']
            })

    return dataset


def load_model(beam_size):
    # Center Identification
    reaction_dataset = dataset.USPTOFull("datasets", 
                atom_feature="center_identification", kekulize=True)
    reaction_train, reaction_valid, reaction_test = reaction_dataset.split()

    reaction_model = models.RGCN(input_dim=reaction_dataset.node_feature_dim,
                    hidden_dims=[512, 512, 512, 512],
                    # hidden_dims=[10, 10],
                    num_relation=reaction_dataset.num_bond_type,
                    concat_hidden=True)
    reaction_task = tasks.CenterIdentification(reaction_model,
                                            feature=("graph", "atom", "bond"))

    # Synthon Completion
    synthon_dataset = dataset.USPTOFull("datasets/", as_synthon=True,
                atom_feature="synthon_completion", kekulize=True)
    synthon_train, synthon_valid, synthon_test = synthon_dataset.split()

    synthon_model = models.RGCN(input_dim=synthon_dataset.node_feature_dim,
                            hidden_dims=[512, 512, 512, 512],
                            # hidden_dims=[10, 10],
                            num_relation=synthon_dataset.num_bond_type,
                            concat_hidden=True)
    synthon_task = tasks.SynthonCompletion(synthon_model, feature=("graph",))

    # Retrosynthesis
    reaction_task.preprocess(reaction_train, None, None)
    synthon_task.preprocess(synthon_train, None, None)
    task = tasks.Retrosynthesis(reaction_task, synthon_task, center_topk=beam_size,
                                num_synthon_beam=beam_size, max_prediction=beam_size)
    optimizer = torch.optim.Adam(task.parameters(), lr=1e-3)
    solver = core.Engine(task, reaction_train, reaction_valid, reaction_test,
                        optimizer, gpus=args.gpus, batch_size=32)
    solver.load("g2gs_reaction_model.pth", load_optimizer=False)
    solver.load("g2gs_synthon_model.pth", load_optimizer=False)

    return task, reaction_dataset


def get_batch(product_smiles, kwargs):
    batch = []
    for i, smiles in enumerate(product_smiles):
        mol = Chem.MolFromSmiles(smiles, sanitize=True)
        if not mol:
            continue
        product = data.Molecule.from_molecule(mol, **kwargs)
        with product.node():
            product.node_label = torch.zeros(product.num_node)
            product.atom_map = torch.arange(product.num_node) + 1
        with product.edge():
            product.edge_label = torch.zeros(product.num_edge)
            product.bond_stereo[:] = 0
        batch.append({
            "graph": (product, product),    # Fake reactant
            "reaction": 0,
            "sample id": i,
        })

    batch = data.graph_collate(batch)
    if args.gpus:
        batch = utils.cuda(batch)
    return batch


def get_prediction(model, batch):
    reactants, num_prediction = model.predict(batch)
    num_prediction = num_prediction.cumsum(0)
    answer = [[]]
    for i, graph in enumerate(reactants):
        if i == num_prediction[len(answer)-1]: 
            answer.append([])
        _reactants = graph.connected_components()[0]
        answer[-1].append([
            sorted([reactant.to_smiles(isomeric=False, atom_map=False, canonical=True) for reactant in _reactants]),
            -graph.logps.item()
        ])
    assert len(answer) == num_prediction.shape[0]
    return answer


def check_reactant_is_material(reactant):
    return Chem.MolToInchiKey(Chem.MolFromSmiles(reactant))[:14] in stock_inchikeys


def check_reactants_are_material(reactants):
    for reactant in reactants:
        if Chem.MolToInchiKey(Chem.MolFromSmiles(reactant))[:14] not in stock_inchikeys:
            return False
    return True


def get_route_result(task):
    max_depth = task["depth"]
    # Initialization
    answer_list = []
    queue = {
        "routes_info": [{"route": [task["product"]], "depth": 0}],  # List of routes information
        "starting_materials": [],
    }
    while True:
        if len(queue) == 0:
            break
        nxt_queue = {}
        routes_info = queue["routes_info"]
        starting_materials = queue["starting_materials"]
        first_route_info = routes_info[0]
        first_route, depth = first_route_info["route"], first_route_info["depth"]
        if depth > max_depth:
            break
        try:
            batch = get_batch([first_route[-1]], reaction_dataset.kwargs)
        except:
            break
        if len(get_prediction(model, batch)[0]) == 0:
            break

        expansion_solution = get_prediction(model, batch)[0][0]
        iter_routes = deepcopy(routes_info)
        iter_routes.pop(0)
        iter_starting_materials = deepcopy(starting_materials)
        expansion_reactants, _ = expansion_solution[0], expansion_solution[1]
        expansion_reactants = sorted(expansion_reactants)
        if check_reactants_are_material(expansion_reactants) and len(iter_routes) == 0:
            answer_list = iter_starting_materials+expansion_reactants
        else:
            for reactant in expansion_reactants:
                if check_reactant_is_material(reactant):
                    iter_starting_materials.append(reactant)
                else:
                    iter_routes = [{"route": first_route+[reactant], "depth": depth+1}] + iter_routes
            nxt_queue = {
                "routes_info": iter_routes,
                "starting_materials": iter_starting_materials
            }
        queue = nxt_queue
            
    answer_keys = set([Chem.MolToInchiKey(Chem.MolFromSmiles(m))[:14] for m in answer_list])

    # Calculate answers
    ground_truth_keys_list = [
        set([
            Chem.MolToInchiKey(Chem.MolFromSmiles(target))[:14] for target in targets
        ]) for targets in task["targets"]
    ]

    for ground_truth_keys in ground_truth_keys_list:
        if ground_truth_keys == answer_keys:
            return max_depth, True

    return max_depth, False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", "--beam_size", help="beam size", type=int, default=5)
    parser.add_argument("-b", "--batch_size", help="batch size", type=int, default=16)
    parser.add_argument("-g", "--gpus", help="device", default=None)

    args, unparsed = parser.parse_known_args()
    args.gpus = utils.literal_eval(args.gpus)
    beam_size = args.beam_size

    torch.manual_seed(42)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(42)
    np.random.seed(42)

    logger = logging.getLogger()
    logger.disabled = True
    model, reaction_dataset = load_model(beam_size)

    stock = pd.read_hdf('datasets/zinc_stock_17_04_20.hdf5', key="table")  
    stockinchikey_list = stock.inchi_key.values
    stock_inchikeys = set([x[:14] for x in stockinchikey_list])

    overall_result = np.zeros((2))
    depth_hit = np.zeros((2, 15))
    test_tasks = load_dataset("test")
 
    for test_task in tqdm(test_tasks):
        result = get_route_result(test_task)
        max_depth, rank = result
        overall_result[1] += 1
        depth_hit[1, max_depth] += 1
        if rank is not None:
            overall_result[0] += 1
            depth_hit[0, max_depth] += 1

    print("overall_result: ", overall_result, 100 * overall_result[0] / overall_result[1])
    print("depth_hit: ", depth_hit, 100 * depth_hit[0, :] / depth_hit[1, :])
