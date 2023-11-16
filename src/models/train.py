import json
import glob
from pathlib import Path
from argparse import ArgumentParser
import tensorflow as tf
from src.models.tf_net import LeelaZeroNet
from src.data.data_pipeline import ARRAY_SHAPES_WITHOUT_BATCH, make_callable


def get_schedule_function(
    starting_lr, reduce_lr_every_n_epochs, reduce_lr_factor, min_learning_rate
):
    def scheduler(epoch):
        num_reductions = int(epoch // reduce_lr_every_n_epochs)
        reduction_factor = reduce_lr_factor**num_reductions
        return max(min_learning_rate, starting_lr / reduction_factor)

    return scheduler


if __name__ == "__main__":
    parser = ArgumentParser()
    # These parameters control the net and the training process
    parser.add_argument("--num_filters", type=int, default=256)
    parser.add_argument("--num_residual_blocks", type=int, default=10)
    parser.add_argument("--se_ratio", type=int, default=8)
    parser.add_argument("--learning_rate", type=float, default=3e-4)
    parser.add_argument("--no_constrain_norms", action="store_true")
    parser.add_argument("--max_grad_norm", type=float, default=5.6)
    parser.add_argument("--mixed_precision", action="store_true")
    parser.add_argument("--reduce_lr_every_n_epochs", type=int)
    parser.add_argument("--reduce_lr_factor", type=int, default=3)
    parser.add_argument("--min_learning_rate", type=float, default=5e-6)
    parser.add_argument("--save_dir", type=Path)
    parser.add_argument("--tensorboard_dir", type=Path)
    # These parameters control the data pipeline
    parser.add_argument("--dataset_path", type=Path, required=True)
    parser.add_argument("--batch_size", type=int, default=1024)
    parser.add_argument("--num_workers", type=int, default=1)
    parser.add_argument("--shuffle_buffer_size", type=int, default=2**19)
    parser.add_argument(
        "--optimizer", type=str, choices=["adam", "lion"], default="adam"
    )
    # These parameters control the loss calculation. They should not be changed unless you
    # know what you're doing, as the loss values you get will not be comparable with other
    # people's unless they are kept at the defaults.
    parser.add_argument("--policy_loss_weight", type=float, default=1.0)
    parser.add_argument("--value_loss_weight", type=float, default=1.0)
    parser.add_argument("--moves_left_loss_weight", type=float, default=0.01)
    args = parser.parse_args()
    if args.mixed_precision:
        tf.keras.mixed_precision.set_global_policy("mixed_float16")
    model = LeelaZeroNet(
        num_filters=args.num_filters,
        num_residual_blocks=args.num_residual_blocks,
        se_ratio=args.se_ratio,
        constrain_norms=not args.no_constrain_norms,
        policy_loss_weight=args.policy_loss_weight,
        value_loss_weight=args.value_loss_weight,
        moves_left_loss_weight=args.moves_left_loss_weight,
    )
    model.load_weights("checkpoints/training_1/cp.ckpt").expect_partial()

    if args.optimizer == "lion":
        try:
            from lion_tf import Lion
        except ImportError:
            raise ImportError(
                "Lion optimizer not installed. Please install it with "
                "pip install git+https://github.com/Rocketknight1/lion-tf.git"
            )
        optimizer = Lion(args.learning_rate, global_clipnorm=args.max_grad_norm)
    else:
        optimizer = tf.keras.optimizers.Adam(
            args.learning_rate, global_clipnorm=args.max_grad_norm
        )
    if args.mixed_precision:
        optimizer = tf.keras.mixed_precision.LossScaleOptimizer(optimizer)
    callbacks = []
    if args.reduce_lr_every_n_epochs is not None:
        scheduler = get_schedule_function(
            args.learning_rate,
            args.reduce_lr_every_n_epochs,
            args.reduce_lr_factor,
            args.min_learning_rate,
        )
        callbacks.append(tf.keras.callbacks.LearningRateScheduler(scheduler, verbose=1))
    if args.save_dir is not None:
        args.save_dir.mkdir(exist_ok=True, parents=True)
        checkpoint_path = args.save_dir / "training_1/cp.ckpt"
        callbacks.append(
            tf.keras.callbacks.ModelCheckpoint(
                filepath=checkpoint_path, save_weights_only=True, verbose=1
            )
        )
    if args.tensorboard_dir is not None:
        args.tensorboard_dir.mkdir(exist_ok=True, parents=True)
        callbacks.append(
            tf.keras.callbacks.TensorBoard(
                log_dir=args.tensorboard_dir, update_freq="batch", histogram_freq=1
            )
        )

    model.compile(optimizer=optimizer, jit_compile=True)
    array_shapes = [
        tuple([args.batch_size] + list(shape)) for shape in ARRAY_SHAPES_WITHOUT_BATCH
    ]
    output_signature = tuple(
        [tf.TensorSpec(shape=shape, dtype=tf.float32) for shape in array_shapes]
    )
    callable_gen = make_callable(
        chunk_dir=args.dataset_path,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        shuffle_buffer_size=args.shuffle_buffer_size,
    )
    dataset = tf.data.Dataset.from_generator(
        callable_gen, output_signature=output_signature
    ).prefetch(tf.data.AUTOTUNE)

    moves = 0
    paths = glob.glob("data/games/*")
    for path in paths:
        with open(path) as f:
            games = json.load(f)
        for game in games:
            if "tcn" in game:
                moves += len(game["tcn"]) / 2

    print("steps_per_epoch:", int(moves / args.batch_size))
    model.fit(
        dataset,
        epochs=99,
        steps_per_epoch=int(moves / args.batch_size),
        callbacks=callbacks,
    )
